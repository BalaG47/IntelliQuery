# IntelliQuery — Unified Core + BGC Profile Ingestion

**Scope:** implementation of Option D from `docs/bgc/BGC_PRODUCT_MATCHING.md` — one logical profile per physical profile, holding the core measurements and the attached synthetic-BGC measurements, each with its own verified pressure axis.
**Date:** 2026-11-23
**Dataset:** `backend/bgc data/bgc raw data/incois/<WMO>/profiles/*.nc` (read-only, never modified)
**Database:** MySQL 8.0.46, `argo_ocean_data_fresh` (development) + disposable verification databases
**Status:** implemented; **controlled sample only** — the corpus was not bulk-ingested

---

## 1. Summary

Before this change the ingestion treated every `N_PROF` entry as its own profile, keyed by `(float_id, cycle_number, profile_index_in_file)`. The investigation showed that a core file's 2–4 `N_PROF` entries are *processing variants of one physical profile*, that the SD/SR products carry one record per profile, and that `profile_index_in_file` therefore collides across products and cannot identify anything.

Ingestion is now grouped by `(float, cycle)`, profiles are matched on `(JULD, DIRECTION)`, one representative core variant is chosen with a documented rule, and the matching BGC record is attached with its own pressure axis and its own provenance. The stored identity is:

```
(float_id, cycle_number, direction, profile_time)
```

Everything was validated on disposable databases and on a 14-file sample; no bulk ingestion was performed, no raw NetCDF file was written to, and no profile association was fabricated.

| Result | Value |
|---|---|
| Logical profiles in the controlled sample | 11 rows from 14 files (13 `N_PROF` records) |
| Controlled-script checks | 221 passed, 0 failed |
| Test suite | 754 tests OK, 9 skipped |
| Development database | V4 applied, 1,384 rows, 0 payload changes, 1,384/1,384 directions recovered |
| Duplicate logical identities | 0 (sample, migration copy and development database) |

---

## 2. Files changed

### New

| File | Purpose |
|---|---|
| `backend/bgc_matching.py` | Product classification, metadata-first corpus index (`netCDF4`), logical signature, representative-variant selection, BGC-record selection, group planning, pressure-grid classification |
| `backend/migrations/V4__logical_profile_identity_and_bgc_axis.sql` | The schema change (§3) |
| `backend/scripts/backfill_direction.py` | Recovers `direction` for pre-V4 rows from the source files they recorded |
| `backend/test_bgc_merged_ingestion.py` | Synthetic rules tests + real-file tests (float 3902581 cycles 1/6, the rescaled-pressure pair, the 12-file sample) |

### Modified

| File | Change |
|---|---|
| `backend/data_processor.py` | Payload construction split out of `_process_single_profile`; new `extract_direction`, `_write_plan`, `process_group`, `process_paths`, `process_dataset`; `process_file` and `process_and_ingest` now group by `(float, cycle)` |
| `backend/database_manager.py` | Identity constant and validation, required-column list, `check_profile_exists(float_id, cycle_number, direction, profile_time)`, narrower-key rejection, ChromaDB document/metadata now carry direction and BGC parameters |
| `backend/scripts/bgc_controlled_ingestion.py` | Rewritten: V4 schema check, migration test on a copy of the development database, merged-row verification against the NetCDF sources, idempotency, core-only regression |
| `backend/test_bgc_readiness.py` | Fakes and identity tests updated to the logical identity |
| `backend/test_bgc_real_files.py` | Multi-`N_PROF` expectations changed from "one row per variant" to "one row per logical profile, variants in provenance" |
| `backend/test_bgc_profile_schema.py` | New identity constant, V4 error messages, checks for the three new columns |
| `docs/bgc/BGC_PRODUCT_MATCHING.md` | Status note so the report no longer reads as an unapplied proposal |

Not touched: raw NetCDF files, Data Explorer, visualisation, chatbot, benchmark code, `main.py`, `ingestion.py` (its `process_file` contract is unchanged).

---

## 3. V4 schema changes

`argo_profiles` grows from 36 to 39 columns.

| Column | Type | Why |
|---|---|---|
| `direction` | `CHAR(1) NOT NULL DEFAULT '-'` | Part of the logical identity; `'-'` is the documented "unknown" value |
| `bgc_pressure` | `JSON NULL` | The BGC product's own pressure axis, stored beside the BGC arrays |
| `bgc_source_metadata` | `JSON NULL` | BGC file, `N_PROF` index, product type, data mode, match rule, grid check |

`direction` is `NOT NULL` on purpose: in MySQL a unique key containing a `NULL` column does not constrain anything, so a nullable direction would silently allow duplicate profiles.

```sql
-- 1. Direction (identity) and the BGC product's own axis / provenance.
ALTER TABLE argo_profiles
  ADD COLUMN direction CHAR(1) NOT NULL DEFAULT '-' AFTER cycle_number,
  ADD COLUMN bgc_pressure JSON DEFAULT NULL AFTER bgc_adjusted_qc,
  ADD COLUMN bgc_source_metadata JSON DEFAULT NULL AFTER bgc_pressure;

-- 2. The logical identity key. Added before the old key is dropped.
ALTER TABLE argo_profiles
  ADD UNIQUE KEY uq_argo_profiles_logical
    (float_id, cycle_number, direction, profile_time);

-- 3. The old key (float_id, cycle_number, profile_index_in_file) would still
--    reject the merge of a core and a BGC record into one row.
ALTER TABLE argo_profiles
  DROP INDEX uq_argo_profiles_identity;
```

Order matters: the wider key is created first so the `float_id` foreign key (`argo_profiles_ibfk_1`) always has an index to use. `profile_index_in_file` is kept, as provenance only. The discarded processing variants are listed in `source_metadata.core_variants` as `{file, profile_index_in_file, data_mode, levels, selected}`.

`DatabaseManager` now refuses to write anything into a database that is not at V4: the identity key must be present, and **no** other unique key may omit part of the identity (a narrower key such as the old `(float, cycle, profile_index_in_file)` would reject a legitimate ascending/descending pair).

---

## 4. Migration validation

### Audit before writing the migration

| Question | Answer |
|---|---|
| Existing rows | 1,384 rows, 22 floats, 36 columns |
| Collisions on `(float, cycle, profile_time)` | **0** — the new key can be created without deleting or rewriting a row |
| `(float, cycle)` multiplicity | 0 groups with more than one row |
| Can `profile_time` be recovered? | Not needed — it was already stored for every row |
| How is legacy `direction` filled? | `DEFAULT '-'`, then `scripts/backfill_direction.py` reads `DIRECTION` from the recorded `source_file` at the stored `profile_index_in_file` |
| Foreign keys / indexes | one FK (`argo_profiles_ibfk_1` on `float_id`), one unique key (`uq_argo_profiles_identity`, V3), one lookup index (`idx_profiles_source_hash`) |

### Test on a disposable copy of the development database

Copy created with `CREATE TABLE ... LIKE` + `INSERT ... SELECT` into `intelliquery_v4_migration_copy`, then V4 applied and re-checked:

| Check | Result |
|---|---|
| Rows copied | 1,384 |
| Collisions on `(float, cycle, profile_time)` | 0 |
| Rows after V4 | 1,384 → 1,384 (unchanged) |
| Payload fingerprint (CRC of identity columns + `LENGTH()` of every JSON payload) | identical before and after |
| Unique keys after V4 | exactly `(float_id, cycle_number, direction, profile_time)` |
| New key accepts ascending + descending of one cycle | yes |
| New key rejects a duplicate logical identity | yes (test transaction rolled back) |
| `direction` backfill | **1,384 recovered, 0 unresolved**, row count unchanged |

Fingerprint values measured on the copy: rows 1384, pressure 1,353,528 bytes, temperature 1,569,851, salinity 1,550,557, BGC 0 — identical before and after, i.e. V4 did not touch a single stored measurement.

The backfill never guesses: a row whose file is missing, whose `N_PROF` index no longer exists, whose direction is not `A`/`D`, or whose observation time disagrees with the stored `profile_time` keeps `'-'` and is reported. Only the `direction` column is ever written.

---

## 5. Matching and merge implementation

### Pipeline

1. **Index.** Every file is opened once with `netCDF4` (xarray costs seconds per file here) and reduced to one metadata record per `N_PROF`: float, cycle, `JULD`, `DIRECTION`, `DATA_MODE`, position, product family, and the number of valid `PRES` levels. Measurement arrays are counted, never kept.
2. **Group.** Records are bucketed by `(float, cycle)`.
3. **Match.** Inside a group, records are bucketed by the physical signature `(round(JULD, 6), DIRECTION)`. `profile_index_in_file` is never used. A record with no `JULD` gets its own signature, because two unknown times are not evidence of the same profile.
4. **Select the core representative.** For the core records of one signature:
   - prefer a delayed-mode (`DATA_MODE = 'D'`) variant that keeps **≥ 90 %** of the maximum level count among the variants;
   - otherwise take the variant with the **most levels**;
   - ties break on data mode (`D` > `R` > `A`), then file name, then `N_PROF` index.
   Every variant — selected or not — is recorded in `source_metadata.core_variants`, so nothing is unrecoverable and the choice is auditable.
5. **Attach the BGC record** with the same signature: `bgc_params`, `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_error`, `bgc_adjusted_qc`, `parameter_data_mode`, plus `bgc_pressure` and `bgc_source_metadata`.
6. **Fallback.** A BGC record without a core counterpart becomes its own row (`match: bgc_only`); a core profile without BGC stays `core_only`. A profile is never matched on index alone and never dropped.

### What the merged row contains

| Column group | Source |
|---|---|
| `pressure`, `temperature`, `salinity` and their QC / adjusted / error / adjusted-QC columns | The selected **core** variant |
| `data_mode`, profile-level QC, `scientific_calib` | The selected **core** variant |
| `direction`, `profile_time`, `cycle_number`, position | The matched pair (same physical profile) |
| `bgc_params`, `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_error`, `bgc_adjusted_qc`, `parameter_data_mode` | The **BGC** product |
| `bgc_pressure` | The **BGC** product's own `PRES` — never the core axis, never interpolated, never reindexed |
| `source_metadata`, `source_file`, `source_file_sha256`, `profile_index_in_file` | The selected core file, plus `core_variants` |
| `bgc_source_metadata` | The BGC file, its `N_PROF` index, product type, data mode, `match`, `grid_check`, `grid_check_detail`, candidate list |

If several BGC products match one signature (not the case in this corpus), one is chosen by documented preference (`BD` > `BR` > `SD` > `SR`) and **all** candidates are listed in the provenance.

Coordinates: the core profile is authoritative. The BGC position is used only when the core profile has no fix at all, and `position_used_for_profile` records that this happened.

Safety: two records resolving to the same identity inside one run are refused and reported rather than inserted. `check_profile_exists(float_id, cycle_number, direction, profile_time)` then makes re-ingestion a no-op.

### Timestamp normalisation

The column is `DATETIME` (whole seconds) and MySQL **rounds** a fractional value rather than truncating it. `profile_time_from_juld()` rounds `JULD` half-up to whole seconds once, and that single value is used both for the insert and for the existence check — which is what makes re-ingestion idempotent.

---

## 6. Pressure validation implementation

`classify_pressure_grid(bgc_pressure, core_pressure)` returns a status plus a detail block (`bgc/core pressure ranges`, `overlap_fraction`, `median_ratio`, `reason`).

| Status | Trigger |
|---|---|
| `missing_pressure` | no pressure array, or no usable (non-null, finite) value |
| `grid_mismatch` | outside −5 … 12 000 dbar; fewer than two levels; non-monotonic axis; overlap below 90 % of the core range |
| `pres_scale_suspect` | the median core/BGC pressure ratio falls between 100× and 10 000× — the ≈1000× defect of the synthetic product |
| `ok` | none of the above |

Behaviour that matters:

- Values are **never** rescaled, shifted or reordered; the BGC array keeps the axis it was delivered with and the flag tells the query layer not to use it for depth work.
- The check never causes a profile to be split or dropped. `SD2902306_001` is attached to its core profile with `grid_check = 'pres_scale_suspect'`, core `pressure` intact.
- The lower bound allows slightly negative near-surface pressure: the real descending profile `SD3902581_001D` starts at **−0.29 dbar** and is correctly reported as `ok` instead of as an anomaly.
- When there is no core profile (the `bgc_only` fallback) only the intrinsic checks run, and no scale judgement is invented.

---

## 7. Tests

### Synthetic (`test_bgc_merged_ingestion.py`)

Logical identity independent of `N_PROF`; ascending and descending profiles of one cycle; multiple core variants; delayed-mode preference at the 90 % threshold; fall-back to the most levels; tie-breaking; core/BGC matching; BGC-only fallback; matching not based on index alone; duplicate logical profiles collapsing into one plan; every grid status (ok, suspect, missing, implausible, insufficient overlap, non-monotonic, single level, no-core); values never rescaled; product-family classification.

### Real files

| Case | Assertion |
|---|---|
| Float 3902581, cycles 1 **and** 6 | 4 rows from 8 files, both directions per cycle, `{1: {A, D}, 6: {A, D}}` |
| Ascending vs descending | descending row keeps `D3902581_001D.nc` as core and `SD3902581_001D.nc` as BGC, `matched`, grid `ok` |
| Multi-profile core file | `D3902581_001.nc`: 4 variants `[243, 2, 476, 494]` levels → 1 row (variant 3 selected); `D3902581_001D.nc`: 3 variants `[224, 1769, 1775]` → 1 row |
| Merged measurements | core arrays present, BGC arrays present, `len(param) == len(bgc_pressure)` for every BGC parameter |
| Known anomaly | `SD2902306_001` → `pres_scale_suspect`, median ratio > 100, axis still ≈0–2 dbar, core pressure still up to > 1000 dbar |
| 12-file controlled sample | 10 rows from 13 `N_PROF` records, no duplicate identity, 7 `bgc_only`, idempotent |
| Core-only regression | `D2902306_001` / `R3902581_059` still ingest as core-only rows with no BGC columns |

### Suite result

```
python -m unittest discover -p "test_*.py"
Ran 754 tests in 162.5 s
OK (skipped=9)
```

Note: `discover -p "test*.py"` additionally picks up the pre-existing ad-hoc script `backend/test.py`, which fails only on Windows `cp1252` console encoding while printing the ChromaDB collection. It is unrelated to this work and was left untouched.

---

## 8. Controlled ingestion counts

`python scripts/bgc_controlled_ingestion.py` — disposable database, 14 sample files:

```
files=14 groups=10 inserted=11 skipped=0 errors=0
checks passed : 221
checks failed : 0
    6 passed,  0 failed  1. Disposable database and V4 schema
   22 passed,  0 failed  2. V4 migration on a copy of the development database
    4 passed,  0 failed  3. Controlled group ingestion (first pass)
  183 passed,  0 failed  4. Stored rows against the NetCDF sources
    4 passed,  0 failed  5. Re-ingestion (idempotency)
    2 passed,  0 failed  6. Core-only ingestion still works
```

| File(s) | What it proves | Rows |
|---|---|---|
| `D3902581_001` + `D3902581_001D` + `SD3902581_001` + `SD3902581_001D` | ascending and descending of one cycle, both merged | 2 |
| `D2902306_001` + `SD2902306_001` | merged with the known rescaled-BGC defect | 1 |
| 7 SD/SR files | `bgc_only` fallback with their own axis | 7 |
| `R6990679_001` (4 `N_PROF`) | variants collapse into one profile | 1 |
| **Total** | **13 `N_PROF` records → 11 logical profiles** | **11** |

Every stored row was then re-checked against its own NetCDF source (not against the ingestion code): core arrays equal the selected variant, BGC arrays equal the BGC file, `len(param) == len(bgc_pressure)` for every BGC parameter, the BGC parameter set equals `STATION_PARAMETERS` minus core/auxiliary, and the recorded grid status matches the measured ranges. `SD2902306_001` is the only flagged profile (`pres_scale_suspect`).

---

## 9. Idempotency

| Run | Result |
|---|---|
| Second pass over the 14 sample files | 0 inserted, 11 skipped, row count still 11 |
| Float 3902581 cycles 1 + 6 (test) | 4 inserted → 0 inserted / 4 skipped |
| 12-file sample (test) | 10 inserted → 0 inserted / 10 skipped |
| Duplicate logical identities after re-ingestion | 0 |

Re-ingestion is a no-op because the identity is the full logical key and `profile_time` is normalised once, on both sides of the comparison.

---

## 10. Development database state

V4 was applied to `argo_ocean_data_fresh` **after** the disposable-copy validation, then the direction backfill was run:

| Property | Before V4 | After V4 + backfill |
|---|---|---|
| Rows | 1,384 | 1,384 |
| Floats | 22 | 22 |
| Columns | 36 | 39 |
| Unique keys | `(float, cycle, profile_index_in_file)` | `(float, cycle, direction, profile_time)` |
| `direction` | column absent | 1,384 × `A` |
| Payload bytes (pressure / temperature / salinity / BGC) | 1,353,528 / 1,569,851 / 1,550,557 / 0 | identical |
| Duplicate logical identities | — | 0 |
| Rows deleted or rewritten | — | 0 |

Cross-check: all **1,384** rows were matched against their **1,384** source files in `backend/argo_data`; the identity computed from each file equals the stored `(float, cycle, direction, profile_time)` in every case (**0 mismatches**). Re-ingesting the existing dataset will therefore skip rather than duplicate.

The 1,384 legacy profiles are all ascending (`A`); the corpus sample in `argo_data` contains no descending core files.

---

## 11. Reproducing

All commands from `backend/` with `DB_USER` / `DB_PASSWORD` / `DB_NAME` in the environment:

```bash
# unit + real-file tests
python -m unittest test_bgc_merged_ingestion test_bgc_real_files
python -m unittest discover -p "test_*.py"

# controlled validation (disposable DBs, ~3–4 min)
python scripts/bgc_controlled_ingestion.py            # drops its temp DB
python scripts/bgc_controlled_ingestion.py --keep-db  # inspect it instead

# schema migration on a real database (validated on a copy first)
python scripts/backfill_direction.py --dry-run
python scripts/backfill_direction.py
```

`backfill_direction.py` accepts `--database`, `--data-dir`, `--dry-run` and `--all`; it prints recovered / unchanged / unresolved counts and one reason per unresolved row.

---

## 12. Remaining limitations

1. **Variant choice is a judgement.** One core variant is stored; the others survive in the corpus and in `core_variants`. The delayed-mode rule keeps adjusted data for most profiles, but **12 profiles** in the corpus have a delayed record below 90 % of the maximum level count, so their adjusted values are not the stored ones. This is the documented rule's cost, not a silent loss.
2. **No `DATA_MODE` in the BGC product.** A merged row keeps the core profile's `data_mode`; `parameter_data_mode` on a merged row is the BGC file's copy (which covers `PRES`/`TEMP`/`PSAL` plus the BGC parameters).
3. **Second-precision identity.** `profile_time` is a whole-second `DATETIME`; a float sampling twice within one second would need a finer key. No collision exists in this corpus or the development database.
4. **The 13 rescaled-`PRES` files** are stored and flagged, but cannot support BGC-vs-depth work until the synthetic product is fixed. The scaling is deliberately *not* corrected, because that would be an unverified scientific change.
5. **Grid thresholds are heuristics** tuned to this corpus: 100×–10 000× median ratio, 90 % core-range overlap, −5 … 12 000 dbar plausibility.
6. **Validation coverage is the sample, not the corpus.** Matching was proven on the 14-file controlled sample and float 3902581 cycles 1–6. A full-corpus run remains untested at scale; `process_dataset` also opens each file twice (`netCDF4` for indexing, xarray for the payload), which is fine for a sample but slow for 1,264 files.
7. **No profile identifier exists** in either product, so association remains derived from metadata (`JULD` + `DIRECTION`), not from an authoritative identifier. A future corpus with real `BD`/`BR` products or different `N_PROF` conventions must re-run the matching analysis.
8. **Cycle counts change.** A cycle can now legitimately hold two rows (ascending + descending). Any query or test that assumed one row per `(float, cycle)` needs the direction in its grouping.
9. **ChromaDB** documents now mention direction and the BGC parameter names, but the embedding model and document structure are otherwise unchanged.
