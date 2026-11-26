# IntelliQuery — Full-Corpus BGC Ingestion Validation

**Scope:** validate ingestion of the complete synthetic BGC-Argo corpus
(1,264 NetCDF files, 632 logical profiles, 8 floats) against an isolated clone of
the development database and an isolated ChromaDB store.

**Date:** 2026-11-26 04:07 UTC
**Dataset:** `C:\AI Projects\IntelliQuery\backend\bgc data\bgc raw data` (read only)
**Tool:** `backend/scripts/bgc_full_corpus_ingestion_validation.py`
**Architecture validated:** `backend/bgc_matching.py`, `backend/data_processor.py`,
`backend/database_manager.py`, migrations V2/V3/V4, ChromaDB document builder.
**Report generated from 14527 executed checks** (2 failed).

---

## 1. Environment isolation and fingerprints

| Item | Value |
|---|---|
| MySQL server | 8.0.46 |
| Host / port | localhost:3306 |
| User | `root` |
| Development database (read only) | `argo_ocean_data_fresh` |
| Validation database (clone + ingestion) | `intelliquery_bgc_full_corpus_check` |
| ChromaDB development store (never opened) | `chroma_db_storage_fresh` |
| ChromaDB validation store | `scratch/bgc_full_validation/chroma_store` |
| ChromaDB collection validated | `argo_float_profiles_full_check` (development collection: `argo_float_profiles`) |
| Dataset root (read only) | `C:\AI Projects\IntelliQuery\backend\bgc data\bgc raw data` |

The validation database was created as a **structural and data clone** of the
development database: each table is re-created from the source `SHOW CREATE
TABLE` text (which, unlike `CREATE TABLE … LIKE`, keeps the foreign key) and
filled with `INSERT … SELECT`, so it starts with the same rows, columns,
indexes, unique key, foreign key and character set. Because the development
database is already migrated, V2/V3/V4 were **verified** on the clone rather
than re-run (re-running a migration that already ran raises on existing
columns).

| Fingerprint | Before |
|---|---|
| Development `argo_profiles` rows / `argo_floats` rows | 1384 / 22 |
| Development payload sums (identity hash, PRES, TEMP, PSAL, BGC) | {'rows_total': 1384, 'identity_hash': Decimal('3050654758879'), 'pres': Decimal('1353528'), 'temp': Decimal('1569851'), 'psal': Decimal('1550557'), 'bgc': Decimal('0')} |
| Development schema signature | 39 columns, 7 index entries |
| Clone rows / floats / schema equal to the development database | pass (rows 1384, floats 22) |
| Development ChromaDB store fingerprint | 6 files, 37202002 bytes, digest a3e41263d9d9640e |
| Corpus files (size + mtime) | 1264 |
| Corpus SHA-256 recorded | 1264 files |

The development database is used only by `SELECT`/`SHOW` statements; every write
in this run goes to `intelliquery_bgc_full_corpus_check`. The configured ChromaDB directory is never
passed to a client: ingestion opens `scratch/bgc_full_validation/chroma_store`
with its own collection.

## 2. Per-float ingestion results

Each float was ingested through `ArgoDataProcessor.process_paths` — the production
entry point — in the audit's float order.

| Float | Files | Groups | Inserted | Skipped | Errors | Wall clock (s) | Stored rows | Directions | Grid status |
|---|---|---|---|---|---|---|---|---|---|
| 2902306 | 170 | 85 | 85 | 0 | 0 | 31419.5 | 85 | {'A': 85} | {'pres_scale_suspect': 10, 'ok': 75} |
| 2903951 | 138 | 69 | 69 | 0 | 0 | 497.0 | 69 | {'A': 69} | {'ok': 69} |
| 3902581 | 236 | 112 | 118 | 0 | 0 | 846.9 | 118 | {'A': 112, 'D': 6} | {'ok': 118} |
| 4903783 | 206 | 103 | 103 | 0 | 0 | 709.9 | 103 | {'A': 103} | {'ok': 103} |
| 6990679 | 52 | 26 | 26 | 0 | 0 | 168.7 | 26 | {'A': 26} | {'ok': 26} |
| 7902170 | 184 | 92 | 92 | 0 | 0 | 574.9 | 92 | {'A': 92} | {'ok': 90, 'pres_scale_suspect': 2} |
| 7902190 | 114 | 57 | 57 | 0 | 0 | 392.1 | 57 | {'A': 57} | {'ok': 57} |
| 7902200 | 164 | 82 | 82 | 0 | 0 | 517.9 | 82 | {'A': 82} | {'ok': 81, 'pres_scale_suspect': 1} |
| **Total** | **1264** | **626** | **632** | **0** | **0** | **35127.0** | **2016** | | |

## 3. Expected versus actual profile counts

| Metric | Expected (audit) | Actual |
|---|---|---|
| NetCDF files | 1264 | 1264 |
| `(float, cycle)` groups | 626 | 626 |
| Logical profiles (plan) | 632 | 632 |
| Rows inserted into the clone | 632 | 632 |
| Rows stored for the corpus floats | 632 | 632 |
| Rows in the validation database (existing + corpus) | 632 + existing | 2016 |
| Matching failures / errors | 0 | 0 |
| Duplicate logical identities | 0 | 0 |

The plan was recomputed from the sources with the production matcher and
compared identity by identity with the full-corpus audit's `plans.json`:
**every float's identities equal the audit's plans**.

## 4. Database identity and overlap analysis

The clone starts with the existing 1384 core profiles of
22 floats from the earlier dataset. Overlap was established from
logical identities, not assumed:

| Measure | Value |
|---|---|
| Existing rows before ingestion | 1384 |
| Existing floats before ingestion | 22 |
| Corpus identities | 632 |
| Corpus identities already present (exact identity match) | 0 |
| New profile identities inserted | 632 |
| Final `argo_profiles` rows | 2016 |
| Final `argo_floats` rows | 30 |
| Existing rows whose payload changed | 0 |
| Identity set equals existing ∪ plan | pass |

The existing rows belong to a different float population (WMO 1900xxx/2900xxx from the earlier real-data download), and no corpus identity existed before ingestion, so the final count is 2016 = 1384 + 632. This was measured, not assumed.

## 5. Source-to-database validation

Every one of the 632 corpus rows was matched back to its selected
core NetCDF `N_PROF` and its BGC `N_PROF`, and every stored value was compared
with the source array (tolerance 1e-6, fill values and NaNs preserved as `NULL`;
1e-4 degrees for the single-precision `FLOAT` position columns).

| Field group | Values checked (all 8 floats) |
|---|---|
| `PRES`, `TEMP`, `PSAL` raw | 1896/1896 |
| Measurement `*_QC` flags | 1896/1896 |
| `*_ADJUSTED` values | 1896/1896 |
| `*_ADJUSTED_ERROR` values | 1896/1896 |
| `*_ADJUSTED_QC` flags | 1896/1896 |
| `DATA_MODE` and `PROFILE_*_QC` | 2528/2528 |
| Latitude / longitude | 1264/1264 |
| `source_metadata` fields | 3160/3160 |
| `source_metadata.core_variants` | 632/632 |
| `bgc_pressure` axis | 632/632 |
| BGC parameter set | 632/632 |
| BGC raw / QC / adjusted / error / adjusted QC | 3160/3160 |
| BGC array lengths equal the axis | 13075/13075 |
| `parameter_data_mode` | 632/632 |

Identity, provenance, variant selection, match status, grid status, SHA-256 and
per-row shape assertions are separate checks; section 15 lists every section's
totals and `scratch/bgc_full_validation/summary.json` holds the machine-readable
counts and mismatch examples.

## 6. Parameter coverage

Only parameters delivered by the source are stored; nothing is invented, and a
parameter absent from a profile is absent from that row.

| Parameter | Profiles carrying it (of 632) |
|---|---|
| DOXY | 619 / 632 |
| CHLA | 619 / 632 |
| BBP700 | 619 / 632 |
| NITRATE | 126 / 632 |
| PH_IN_SITU_TOTAL | 316 / 632 |

Every value equals the audit's per-parameter profile count
(D OXY 619, CHLA 619, BBP700 619, NITRATE 126, PH_IN_SITU_TOTAL 316):
**pass**.

## 7. Pressure-grid anomaly verification

| Grid status | Profiles |
|---|---|
| ok | 619 |
| pres_scale_suspect | 13 |

The 13 `pres_scale_suspect` profiles:

| Float | Cycle | Dir | BGC file | Core max (dbar) | BGC max (dbar) | Median ratio | BGC levels |
|---|---|---|---|---|---|---|---|
| 2902306 | 1 | A | `SD2902306_001.nc` | 1995.77 | 1.996 | 1000.01 | 996 |
| 2902306 | 11 | A | `SD2902306_011.nc` | 1995.95 | 1.996 | 998.981 | 996 |
| 2902306 | 16 | A | `SD2902306_016.nc` | 1997.27 | 1.997 | 1000.005 | 998 |
| 2902306 | 17 | A | `SD2902306_017.nc` | 1995.87 | 1.996 | 1000.0 | 997 |
| 2902306 | 18 | A | `SD2902306_018.nc` | 1995.95 | 1.996 | 999.99 | 997 |
| 2902306 | 23 | A | `SD2902306_023.nc` | 1997.3 | 1.997 | 1000.005 | 998 |
| 2902306 | 24 | A | `SD2902306_024.nc` | 1995.87 | 1.996 | 1000.02 | 997 |
| 2902306 | 25 | A | `SD2902306_025.nc` | 1995.4399 | 1.995 | 999.031 | 996 |
| 2902306 | 26 | A | `SD2902306_026.nc` | 1995.89 | 1.996 | 1000.0 | 997 |
| 2902306 | 39 | A | `SR2902306_039.nc` | 1997.3 | 1.997 | 1000.02 | 998 |
| 7902170 | 22 | A | `SR7902170_022.nc` | 1997.21 | 1.997 | 999.995 | 998 |
| 7902170 | 26 | A | `SR7902170_026.nc` | 1995.09 | 1.995 | 1000.0 | 997 |
| 7902200 | 15 | A | `SR7902200_015.nc` | 1995.8199 | 1.996 | 999.95 | 997 |

* Exactly 13 rows carry `pres_scale_suspect`, and their
  identities equal the audit's list: **pass**.
* Their BGC axes are stored exactly as delivered (`bgc_pressure` equals the BGC
  file's `PRES` array, maxima ≈1.995–1.997 dbar) while their core axes keep the
  real 0–2,000 dbar values: **pass**.
* Their BGC measurements remain stored (`bgc_params` / `bgc_adjusted` match the
  source): **pass**.
* The other 619 profiles are `ok`, and no profile is
  `missing_pressure` or `grid_mismatch`.
* The flag is queryable in SQL without a code change
  (`JSON_EXTRACT(bgc_source_metadata, '$.grid_check')`), so a BGC-versus-depth
  consumer can exclude those 13 rows. Nothing was rescaled,
  shifted or corrected.

## 8. ChromaDB integration results

The production vectorization path ran unchanged: `_store_payload` calls
`DatabaseManager.add_profile_to_chromadb`, which builds the document with
`_create_profile_document`, embeds it with `all-MiniLM-L6-v2` and adds it to the
collection. Only the client's directory and collection name were pointed at the
validation store.

| Measure | Value |
|---|---|
| Vectors in the validation collection | 632 |
| Rows inserted in this run | 632 |
| Collection empty before the run | pass |
| Vector ids equal relational `profile_id`s | pass (0 missing, 0 extra) |
| Duplicate vector records | 0 |
| Metadata matches relational identity (id, float, cycle, direction, date, position) | pass |
| Document names its own profile | pass |
| BGC parameters listed in the document | pass |
| Document hashes stable across re-ingestion | pass |
| Development ChromaDB store unchanged | pass |
| Existing core vector records deleted or overwritten | none possible: the validation store is a separate directory and a separate collection |

## 9. Vector metadata and idempotency

| Measure | Value |
|---|---|
| Metadata keys per document | `cycle_number`, `date`, `direction`, `float_id_sql`, `latitude`, `longitude`, `profile_id_sql` |
| Keys that distinguish core from BGC data | **none** |
| Second vectorization pass added records | 0 |
| Documents changed on the second pass | 0 |
| Vectors before / after the second pass | 632 / 632 |

The BGC distinction exists in the **document text** (every BGC-bearing document
carries a `Bio-geochemical Parameters` block listing `DOXY`, `CHLA`, `BBP700`,
`NITRATE`, `PH_IN_SITU_TOTAL`), which is what the embedding encodes, and BGC
parameter names reach the embedding because `add_profile_to_chromadb` receives
`bgc_keys`. It does **not** exist as a metadata field, so the vector store cannot
filter or count core versus BGC documents with a `where` clause. That is a
retrieval-layer limitation, not an ingestion failure; extending retrieval was out
of scope for this phase.

## 10. Relational database idempotency

The whole corpus was ingested a second time with the identical file list.

| Measure | Value |
|---|---|
| Second-pass inserted | 0 |
| Second-pass skipped | 632 |
| Second-pass errors | 0 |
| Rows before / after | 2016 / 2016 |
| Payload digests changed (every stored column) | 0 |
| `ingestion_timestamp` rewrites | 0 |
| Vectors before / after | 632 / 632 |
| Duplicate identities after re-run | 0 |

Interruption and recovery, measured on a separate throwaway database with float
6990679 (26 profiles). The experiment was run twice, because the first attempt's
delay was too short to reach the ingestion:

| Measure | Run at 45 s | Re-run at 110 s |
|---|---|---|
| Rows committed when the kill arrived | 0 / 26 | 17 / 26 |
| Partially written rows | 0 (nothing was committed) | 0 (every committed row is complete) |
| Planned rows only | n/a (no rows) | yes (no foreign identity) |
| Vectors at the kill | 0 for 0 rows | 17 for 17 rows |
| Rows whose vector was missing at the kill | 0 | 0 |
| Re-run inserted / skipped | 26 / 0 | 9 / 17 |
| Rows after the re-run | 26 (exactly once each) | 26 (exactly once each) |
| Vectors after the re-run | 26 for 26 rows | 26 for 26 rows |

A crash loses no committed row and never leaves half a row: one insert writes one
row and commits immediately, and the re-run skips what already exists. The
**vector store is the exception**: the row is committed *before* its vector is
written, so a kill between the two would leave a row with no vector, and because
the re-run skips existing rows it would not backfill it. The 45 s attempt was killed while the worker was still starting up, so no row had
been committed and the partial-commit case was not exercised — that is the single
Phase 7 check that failed in the run recorded above. The re-run used the
validator's own `recovery_phase` (same subprocess worker, same resume through
`IsolatedChromaManager`) with an interrupt delay of 110 s, killed the worker after
17 profiles had been committed, and passed all eight recovery checks; the evidence
is in `scratch/bgc_full_validation/recovery_probe.log` and `recovery_probe.json`.

Relational recovery is safe. Vector recovery would not be automatic if the kill
landed between the row commit and the vector write; neither attempt caught that
window.

## 11. Regression test results

Command: `python -m unittest discover -p "test_*.py"` from `backend/`.

`Ran 754 tests in 169.786s`

`OK (skipped=9)`

BGC-specific modules: `python -m unittest test_bgc_merged_ingestion test_bgc_readiness test_bgc_profile_schema test_bgc_real_files`

`Ran 111 tests in 165.918s`

`OK`

Logs: `scratch/bgc_full_validation/unit_tests.log` and `bgc_tests.log`.

## 12. Development environment unchanged verification

| Measure | Before | After |
|---|---|---|
| Development `argo_profiles` rows | 1384 | 1384 |
| Development `argo_floats` rows | 22 | 22 |
| Development payload sums | {'rows_total': 1384, 'identity_hash': Decimal('3050654758879'), 'pres': Decimal('1353528'), 'temp': Decimal('1569851'), 'psal': Decimal('1550557'), 'bgc': Decimal('0')} | {'rows_total': 1384, 'identity_hash': Decimal('3050654758879'), 'pres': Decimal('1353528'), 'temp': Decimal('1569851'), 'psal': Decimal('1550557'), 'bgc': Decimal('0')} |
| Development schema | unchanged | unchanged |
| Development ChromaDB store | 6 files, 37202002 bytes, digest a3e41263d9d9640e | 6 files, 37202002 bytes, digest a3e41263d9d9640e |
| Corpus files (size + mtime) | 1264 | 1264 |
| Corpus SHA-256 | 1264 files | 1264 identical |
| Ingestion code and migrations | fingerprinted | unchanged (pass) |

The raw NetCDF files were only opened for reading, and no configuration file was
redirected: the isolation is done in the script (database name, ChromaDB path and
collection are arguments to the managers it constructs), not in `config.py` or
`.env`.

## 13. Defects and fixes

**No production code was changed.** The relational pipeline stored the whole
corpus without a single mismatch, a single duplicate or a single error.

Two integration limitations were found rather than fixed:

1. **No core/BGC metadata key.** The document text lists the BGC parameters and
   the embedding therefore separates BGC-bearing profiles from core-only ones,
   but the ChromaDB metadata has no `bgc`/`product_type` field, so retrieval
   cannot filter on BGC presence. Fixing this is a retrieval-layer change.
2. **The vector write is not transactional with the row insert.** The row is
   committed before its vector is written, so a kill exactly between the two
   would leave a row without a vector, and a re-run skips existing rows instead
   of backfilling them. Neither interruption attempt landed in that window
   (17 rows and 17 vectors at the kill; 26 rows and 26 vectors after the re-run),
   so this is a structural risk rather than an observed gap (section 10).
   A targeted fix is possible (upsert the vector for an already stored row, or
   reconcile on start-up) but it changes the write path and was kept out of this
   validation phase.

## 14. Runtime and resource usage

| Phase | Time |
|---|---|
| Corpus index + hashing (1,264 files) | 5.7 s |
| Ingestion (all 8 floats) | 35157.2 s |
| Row validation against the sources | 4683.2 s |
| Second ingestion pass (idempotency) | 39109.9 s |
| ChromaDB query validation | 0.3 s |
| Interruption and recovery | 158.8 s |
| Regression tests | 384.9 s |
| **Total wall clock** | **79519.7 s** |

| Resource | Value |
|---|---|
| Ingestion of the seven uninterrupted floats | 3,707.4 s for 547 profiles (≈ 6.8 s each) |
| Corpus size | 257.1 MB |
| Peak traced process RSS | 781.8 MB |
| Rows in the validation database at the end | 2016 |

These are wall-clock durations, and the host was suspended twice while the run
was in progress: between 11:32 and 20:09 on 2026-11-25 (during float 2902306) and
between 22:37 on 2026-11-25 and 08:44 on 2026-11-26 (during the second ingestion
pass). The gaps are visible in `scratch/bgc_full_validation/monitor.log`. Float
2902306's 31,419.5 s therefore includes about 8.6 h of suspension and the
39,109.9 s second pass includes about 10.1 h, so the phase times and the total
are not performance measurements and no meaningful profiles-per-second rate can
be derived from them. The 3,707.4 s figure sums the seven floats that ran without
interruption and is the only timing in this table that excludes suspension.

## 15. Checks and final verdict

**14527 checks executed, 14525 passed, 2 failed.**

| Section | Passed | Failed |
|---|---|---|
| Setup: corpus and development fingerprints | 5 | 0 |
| Phase 1: cloned database and migration verification | 15 | 0 |
| Phase 2: isolated vector store | 3 | 0 |
| Phase 3: full-corpus ingestion | 44 | 0 |
| Phase 4: stored rows against the sources | 14424 | 0 |
| Phase 5: vector store validation | 7 | 1 |
| Phase 6: idempotency | 9 | 0 |
| Phase 7: transaction and recovery behaviour | 7 | 1 |
| Phase 8: development environment unchanged | 9 | 0 |
| Phase 9: regression tests | 2 | 0 |

Failed checks:

| Section | Check | Detail |
|---|---|---|
| Phase 5: vector store validation | vector metadata carries a core/BGC distinction of its own | metadata keys: ['cycle_number', 'date', 'direction', 'float_id_sql', 'latitude', 'longitude', 'profile_id_sql'] |
| Phase 7: transaction and recovery behaviour | the run was interrupted after some rows were committed | killed=True rows=0/26 |

Both failures were investigated. The Phase 7 failure is a harness timing artifact:
at a 45 s delay the worker had not yet committed a row, so the partial-commit case
was never reached; re-running the same experiment with a 110 s delay committed 17
of 26 rows and passed all eight recovery checks (section 10), so recovery is
measured rather than assumed. The Phase 5 failure is a real limitation of the
retrieval layer and is the one issue that remains open.

### Verdict

**PASS WITH ISSUES** — the complete corpus was ingested into the isolated clone and
verified against every source file, re-ingestion changed nothing, and an interrupted
run resumed exactly (section 10), but one integration limitation remains open: the
vector metadata carries no core/BGC distinction, so the vector store cannot filter
or count core versus BGC documents.
