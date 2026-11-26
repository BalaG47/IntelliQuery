# BGC ChromaDB Metadata and Missing-Vector Recovery Hardening

**Scope:** the two limitations left open by
`docs/bgc/BGC_FULL_CORPUS_INGESTION_VALIDATION.md`:

1. ChromaDB metadata could not distinguish core-only from core+BGC profiles.
2. A profile committed to MySQL before its vector was written could keep
   that row while losing its vector permanently, because re-ingestion skipped
   an existing row.

**Environment:** MySQL 8.0.46, Python 3.12.10, ChromaDB 1.5.9,
`sentence-transformers` `all-MiniLM-L6-v2`, embedding dimension 384.

**Verdict: PASS WITH ISSUES.** Both fixes are implemented and validated,
including the exact commit/vector-write crash window and the full 1,264-file
corpus. The remaining issues are listed in section 12; none of them is a
retrieval or ingestion regression.

The representative-only run reported **55/55 checks passed**. The full-corpus
run reported **71/73**: every metadata and reconciliation check passed, and
the two totals that differ from the earlier phase are explained and located
in section 9. The whole regression suite reported **784 tests, OK
(skipped=9)**.

## 1. Root cause of the missing BGC metadata

`DatabaseManager.add_profile_to_chromadb` received `bgc_keys` and passed them
to `_create_profile_document`, which wrote a
`Bio-geochemical Parameters` block into the document text. The embedding
therefore saw the parameter names, but the `metadata` dictionary was built
inline from identity and position only:

```python
metadata = {
    "profile_id_sql": int(profile_id_db),
    "float_id_sql": int(float_id_db),
    "cycle_number": int(cycle),
    "date": time.strftime("%Y-%m-%d")
}
if lat is not None and lon is not None: ...
if direction in ("A", "D"): ...
```

No key recorded whether a profile had BGC data, which parameters it carried,
or which product it represented. A ChromaDB `where` filter can only use
metadata, so core-only and core+BGC vectors were indistinguishable to
retrieval even though their text differed.

The list of parameters was also not a property of the stored row at vector
time: it came from the ingestion payload, which is not available to any
consumer that only has the collection.

## 2. Metadata schema before and after

ChromaDB metadata values must be scalars. The installed 1.5.9 accepts a list
value for storage, but a list cannot be used with `where` for containment, so
it would not satisfy "metadata-based BGC filtering". `bgc_parameters` is
therefore a **sorted, comma-separated string** and `bgc_parameter_count` is a
separate integer for range filters. This choice is documented in
`_build_profile_metadata`.

| Key | Before | After | Type |
|---|---|---|---|
| `profile_id_sql` | yes | yes, unchanged | int |
| `float_id_sql` | yes | yes, unchanged | int |
| `cycle_number` | yes | yes, unchanged | int |
| `date` | yes | yes, unchanged | str |
| `latitude` / `longitude` | yes when a fix exists | unchanged | float |
| `direction` | yes for A/D | unchanged | str |
| `has_bgc` | — | new: `true` iff the row has ≥1 BGC parameter | bool |
| `product_type` | — | new: `core` or `core_bgc` | str |
| `bgc_parameters` | — | new: sorted CSV of stored parameter names, `""` for core-only | str |
| `bgc_parameter_count` | — | new: number of names | int |

`product_type` describes the **stored profile content**, not the source file
name: `core_bgc` when the row's `bgc_params` object is non-empty, `core` when
it is empty. A profile ingested from a BGC product file but carrying no BGC
measurement arrays would be `core`; that case does not occur in this corpus
(every BGC-product row carries measurements). The constants are
`DatabaseManager.PRODUCT_TYPE_CORE` and
`DatabaseManager.PRODUCT_TYPE_CORE_BGC`.

`float_id_sql` keeps its existing meaning — the **WMO number** used in the
document's `Float WMO ID` line — although the key name suggests the internal
`argo_floats.float_id`. It was left unchanged to avoid breaking existing
filters and tests.

## 3. Root cause of the missing-vector recovery gap

`ArgoDataProcessor._store_payload` did this:

```python
if self.db_manager.check_profile_exists(*identity):
    return "skipped"

profile_id_db = self.db_manager.insert_profile(payload)   # commits
self.db_manager.add_profile_to_chromadb(...)              # may fail / crash
```

`insert_profile` commits immediately. If the process dies after that commit
and before the `add` completes, the relational row survives and the vector
does not. `check_profile_exists` reduced the row to a boolean
(`SELECT 1`), so a later run could neither detect the missing vector nor learn
the `profile_id` needed to rebuild it: it returned `"skipped"` and the gap was
permanent. Nothing in the codebase compared the relational rows with the
collection, so there was no path to repair it.

## 4. Changes made

### `backend/database_manager.py`

* `PRODUCT_TYPE_CORE` / `PRODUCT_TYPE_CORE_BGC` constants.
* `_bgc_parameter_names(bgc_keys)` — sorted, de-duplicated names; accepts a
  mapping or an iterable.
* `_build_profile_metadata(...)` — the single place the metadata schema is
  defined; legacy keys and the missing-position/unknown-direction rules are
  preserved.
* `_build_profile_vector_payload(...)` — deterministic `(id, document,
  metadata)` for one profile; the document still lists the BGC parameters and
  `add_profile_to_chromadb` still embeds it.
* `_embed_document(document)` — one embedding call site.
* `get_profile_id(...)` — returns the `profile_id` (not a boolean);
  `check_profile_exists` now delegates to it.
* `get_profile_vector_record(profile_id)` — reads the authoritative values a
  vector is rebuilt from, joining `argo_floats` for the WMO number. It never
  writes measurement or identity columns.
* `add_profile_from_row(profile_id)` — builds and writes a vector from the
  committed row. Used for every new insert so a fresh vector and a repaired
  vector are produced from identical inputs.
* `sync_profile_vector(profile_id)` — returns `unchanged`, `recreated` or
  `updated`. It checks one id with `collection.get(ids=[...])` (a point
  lookup, not a collection scan), writes nothing when document and metadata
  already match, and uses `update` for a stale vector so the deterministic id
  cannot create a duplicate. Failures propagate.

### `backend/data_processor.py`

* New `_reconcile_existing_vector(identity)`: resolves the existing row's id
  and calls `sync_profile_vector`. A repair result other than `unchanged` is
  logged.
* `_store_payload` on an existing row reconciles and still returns
  `"skipped"`, so row counts and existing callers are unchanged. A failed
  reconciliation is raised: `process_group` records it as an error and the
  next run retries it.
* A new row is inserted, then `add_profile_from_row(profile_id)` writes its
  vector.

### Why the insert path changed too

The first version kept the old payload-based write and added reconciliation.
The isolated run showed that a re-run then rewrote **10 of 11** vectors that
already existed and matched. Two inputs differed between the payload and the
row:

* metadata carried the WMO number (`float_id_sql`), while the row only exposed
  the internal `argo_floats.float_id`;
* `latitude`/`longitude` are `FLOAT` columns, so the stored values are
  single-precision (`67.73276166666666` became `67.7328`), and the document
  prints the coordinates.

Building both the first write and every repair from the committed row removes
that difference. After the change the second run reported
`{"recreated": 1, "unchanged": 10}` and the third run
`{"unchanged": 11}` with no vector write at all.

### Test doubles

`add_profile_from_row` was added to the fake database managers in
`test_bgc_merged_ingestion.py`, `test_bgc_readiness.py` and
`test_bgc_real_files.py` so they keep recording vector writes. The disposable
managers in `scripts/` inherit the real implementation and needed no change.

## 5. Failure-injection test evidence

The failure window was reached deterministically by an exception, never by a
sleep or a process kill. The isolated manager raises inside
`add_profile_to_chromadb` for one exact profile time, which is after
`insert_profile` has committed:

```python
def add_profile_to_chromadb(self, profile_id, ..., time, ...):
    if self.fail_vector_time is not None and time == self.fail_vector_time:
        self.crashed_profile_ids.append(int(profile_id))
        raise InjectedVectorFailure(...)
    DatabaseManager.add_profile_to_chromadb(self, ...)
```

Measured on the disposable database and store
(`scripts/bgc_chromadb_hardening_validation.py`):

| Evidence | Result |
|---|---|
| Target profile resolved from `D3902581_001.nc` | `2024-01-13 13:31:00` |
| Injected failure reached at the boundary | profile id `5` |
| Run summary | inserted=10, skipped=0, errors=1 |
| Relational rows after the crash | 11 |
| Vectors after the crash | 10 |
| Rows without a vector | exactly `[5]` |
| The committed row's `pressure` / `bgc_params` | complete, non-empty |

The row exists with all its measurements and its id was reported by the
exception. The vector for that id is absent. This is the exact state the old
code could not recover from.

## 6. Recovery and idempotency results

| Step | Measured result |
|---|---|
| Second ingestion run over the same 14 files | inserted=0, skipped=11, errors=0 |
| Stored payload fingerprint | unchanged |
| Duplicate logical identities | none |
| Vectors after the re-run | 11 for 11 rows |
| Reconciliation statuses | `recreated: 1`, `unchanged: 10` |
| Vector metadata vs relational row | 0 mismatched |
| Documents | every document names its profile and its parameters |
| Third run | inserted=0, skipped=11, errors=0 |
| Third-run reconciliation statuses | `unchanged: 11` |
| Vector store digest | unchanged on the third run |
| Vector writes after the first run | none |

Failed reconciliation and retry:

| Step | Measured result |
|---|---|
| One vector deleted (`profile_1`) | row intact, vector absent |
| Run with reconciliation forced to fail | 1 error reported, 0 rows inserted, vector still absent |
| Retry with the failure removed | `recreated: 1`, `unchanged: 10`, 11 vectors |
| Rows | still 11, no duplicate identity |

The whole representative set produced one `core` profile (a core-only source
file) and ten `core_bgc` profiles.

## 7. Core retrieval regression results

* A semantic query (`"oxygen and chlorophyll measurements near the surface"`)
  returned 5 results; every result exposed `profile_id_sql` and all 5 ids
  resolved to `argo_profiles` rows.
* `collection.get(where={"has_bgc": False})` still returns the core-only
  profile, so core retrieval is not hidden by the new metadata.
* The document text and embedding still contain `DOXY`, `CHLA`, `BBP700`,
  `NITRATE` and `PH_IN_SITU_TOTAL` where present (checked in section 6 and by
  `test_chromadb_bgc_metadata`).
* Existing metadata keys are present on every vector, and their values are
  unchanged.

## 8. BGC metadata filtering results

On the isolated collection (11 representative profiles):

| Filter | Expected | Measured |
|---|---|---|
| `has_bgc = true` | 10 BGC-bearing profiles | 10 ids, exact set |
| `has_bgc = false` | 1 core-only profile | 1 id, exact set |
| `product_type = core` | 1 | 1 id, exact set |
| `product_type = core_bgc` | 10 | 10 ids, exact set |
| `bgc_parameters = "<sorted CSV>"` | the matching profile | present |
| `bgc_parameter_count >= n` | profiles with at least n parameters | present |
| `bgc_parameters` vs `JSON_KEYS(bgc_params)` for every BGC row | equal | equal for all 10 |

`test_chromadb_bgc_metadata` additionally checks a three-record collection
(one core-only, two BGC with different parameter sets) and asserts the scalar
type of every metadata value.

## 9. Full-corpus compatibility results

Run: `python scripts/bgc_chromadb_hardening_validation.py --full-corpus`
against a fresh disposable database and a fresh isolated store, using the
current code. It ingested all 1,264 NetCDF files through the production
ingestion and reconciliation paths in **2,638.7 s** (whole run 2,864.4 s).
Evidence: `backend/scratch/bgc_hardening/checks.json` (`full_corpus` key) and
`backend/scratch/bgc_hardening/run.log`.

| Check | Measured |
|---|---|
| Files | 1,264 |
| (float, cycle) groups | 626 |
| Inserted / skipped | 621 / 11 |
| Rows after ingestion | 632 |
| Vectors after ingestion | 632 |
| Duplicate logical identities | 0 |
| Vector ids equal relational ids | exact set (632 / 632) |
| Metadata vs every relational row | 0 mismatched |
| Document names profile and parameters | 0 problems |
| Product types | `core_bgc` 631, `core` 1 |
| Per-parameter coverage | DOXY 618, CHLA 618, BBP700 618, NITRATE 126, PH_IN_SITU_TOTAL 316 |
| `pres_scale_suspect` rows | 12 |
| Three vectors deleted, reconciliation | `recreated: 3`, `unchanged: 629` |
| Relational fingerprint after reconciliation | unchanged |
| Vector digest after reconciliation | identical to before the deletion |
| Second reconciliation pass | `unchanged: 632` |
| Checks in this run | **71 passed / 2 failed (of 73)** |

### The two deviations, and why they are a harness artifact

The script runs its 11-profile representative phase first, in the same
database, and only then ingests the corpus. Two of those 11 profiles were
taken from a *partial* file set of their cycle, so their plan differs from the
plan of the complete cycle; the corpus run then skips the already-present
identity. With the complete group:

| Group | Representative subset plan | Complete group plan |
|---|---|---|
| `6990679` cycle 1 | 4 core variants, 0 BGC -> `core_only` | 4 core + 1 BGC -> `matched` |
| `7902200` cycle 15 | 0 core + 1 BGC -> `bgc_only` | 1 core + 1 BGC -> `matched` |

Consequences in the measured corpus totals:

* `6990679` cycle 1 was stored core-only by the representative phase, so the
  corpus kept it and one profile lacks DOXY/CHLA/BBP700 (618 instead of 619).
  This is also the single `core` vector.
* `7902200` cycle 15 was stored `bgc_only`, so its grid check ran without a
  core pressure axis and stayed `ok`; the corpus kept that row, so the suspect
  count is 12 instead of 13. This is the only pre-seeded cycle among the 13
  suspects (`7902170`'s suspects are cycles 22 and 26; `2902306` cycle 1 was
  merged even in the subset).

A targeted ingestion of just those two complete groups (4 files, 2 rows) into
a fresh disposable database and collection produced the expected result:

| Row | BGC parameters | `grid_check` | `has_bgc` / `product_type` |
|---|---|---|---|
| `6990679` cycle 1 | BBP700, CHLA, CHLA_FLUORESCENCE, DOXY | `ok` | true / `core_bgc` |
| `7902200` cycle 15 | PH_IN_SITU_TOTAL | `pres_scale_suspect` | true / `core_bgc` |

So the two affected profiles do receive the expected metadata when their
cycle's files are ingested together, which is what production ingestion does.
The other 630 profiles match the earlier corpus totals exactly, and every
metadata, uniqueness, reconciliation and vector-digest check passed for all
632. No second full-corpus ingestion was run to obtain this: the run above is
the single corpus run of this phase, and the follow-up check is four files.

### Code fingerprint

* `scripts/bgc_chromadb_hardening_validation.py` — SHA-256 `534407b1…feeea`,
  written 10:34:09, run started 10:34:14.
* `data_processor.py` — SHA-256 `ec6da436…ababe`, written before the run.
* `database_manager.py` — SHA-256 `7de9f13e…d60fe`. It was edited once after
the run started, at 10:46, and that edit changed only the three docstring
lines of `_bgc_parameter_names`; no executable statement changed.
* The final 55/55 representative run used the same executable code.

## 10. Test counts and failures

| Suite | Command | Result |
|---|---|---|
| New metadata tests | `python -m unittest test_chromadb_bgc_metadata` | 18 tests, OK |
| New reconciliation tests | `python -m unittest test_chromadb_vector_reconciliation` | 12 tests, OK |
| Targeted BGC + vector tests | `unittest test_chromadb_bgc_metadata test_chromadb_vector_reconciliation test_bgc_merged_ingestion test_bgc_readiness test_bgc_real_files` | **130 tests, OK (163.1 s)** |
| Full regression | `python -m unittest discover -p "test_*.py"` | **784 tests, OK (skipped=9), 149.3 s** |

The previous baseline was 754 passed / 9 skipped. The difference is exactly
the 30 new tests; no existing test was removed, skipped, weakened or edited
except to add the fake `add_profile_from_row` method described in section 4.

## 11. Files modified

Production code:

* `backend/database_manager.py`
* `backend/data_processor.py`

Tests:

* `backend/test_chromadb_bgc_metadata.py` (new, 18 tests)
* `backend/test_chromadb_vector_reconciliation.py` (new, 12 tests)
* `backend/test_bgc_merged_ingestion.py`
* `backend/test_bgc_readiness.py`
* `backend/test_bgc_real_files.py`

Verification tooling and evidence:

* `backend/scripts/bgc_chromadb_hardening_validation.py` (new)
* `backend/scratch/bgc_hardening/` — run logs, `checks.json`, isolated store
  (git-ignored evidence)
* `docs/bgc/BGC_CHROMADB_HARDENING_REPORT.md` (this report)

Not modified: `config.py`, `.env`, migrations, `argo_ocean_data_fresh`, the
development ChromaDB store, the NetCDF corpus, QuerySpec, the query planner
and the chatbot.

## 12. Remaining limitations

1. **`bgc_parameters` is a CSV string, not a list.** ChromaDB 1.5.9 stores
   lists, but `where` cannot test list membership, so a filter can only match
   the exact parameter set. `has_bgc` and `bgc_parameter_count` cover the
   common cases.
2. **Legacy vectors have no BGC metadata.** The development collection was not
   touched. Rows already in the development database keep their old vectors
   until they are reconciled; a one-time pass calling
   `sync_profile_vector(profile_id)` is required after promotion if their
   metadata should be enriched. Nothing in this phase ran that against the
   development store.
3. **Reconciliation costs one point lookup per existing profile per run.** It
   does not scan the collection and does not re-embed an unchanged vector, but
   a 632-profile re-ingestion performs 632 `get(ids=[...])` calls.
4. **An insert costs one extra `SELECT`.** The vector is built from the
   committed row instead of the payload; that is what makes a fresh vector and
   a repaired vector identical.
5. **`float_id_sql` still holds the WMO number**, not
   `argo_floats.float_id`. The name is historical; changing it would break
   existing filters and was out of scope.
6. **The 13 `pres_scale_suspect` pressure grids are still stored as
   delivered** and still flagged in `bgc_source_metadata`. The source-data
   defect was not fixed, as required.
7. **No development-database ingestion.** The corpus was ingested only into
   disposable environments.
8. **A row stored from a partial file set is not upgraded later.** If a cycle
   is ingested before all of its files are known (a core file whose BGC file
   arrives later), the row keeps the plan it had; the later corpus run skips
   the identity. That is the pre-existing matching/identity behaviour, not a
   vector problem, and changing it would mean re-planning existing rows, which
   is outside this phase. Section 9 shows the effect on two profiles and
   proves that the complete-group plan is correct. Promotion should ingest a
   cycle's files together.

## Verdict

**PASS WITH ISSUES — ChromaDB metadata and missing-vector recovery are
validated; promotion needs the one-time backfill described in section 12.**

* The exact MySQL-commit / ChromaDB-failure scenario was reproduced
  deterministically and recovered.
* The full-corpus run produced 632 rows and 632 vectors with correct metadata
  for 630 profiles; the two deviations from the earlier totals are proven
  partial-group pre-seed effects in the harness, and the complete-group plans
  for both affected cycles were verified directly.
* BGC metadata filtering works (`has_bgc`, `product_type`,
  `bgc_parameters`, `bgc_parameter_count`).
* Existing core retrieval still works and every result resolves to a
  relational row.
* The original development database, ChromaDB store and corpus are unchanged.
* Ready to proceed with development database ingestion **after** deciding how
  the existing development rows' vectors get their new metadata
  (`sync_profile_vector` provides the mechanism; it has not been run there).
