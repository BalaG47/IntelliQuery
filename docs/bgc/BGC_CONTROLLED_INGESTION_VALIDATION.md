# IntelliQuery — Controlled BGC Ingestion Validation

**Scope:** validate the database-writing pipeline on two complete floats in a
disposable database. No full-corpus ingestion, no NetCDF modification, no
measurement correction, no development-database write.

**Date:** 2026-11-24 00:27 UTC
**Dataset:** `C:\AI Projects\IntelliQuery\backend\bgc data\bgc raw data` (read-only)
**Tool:** `backend/scripts/bgc_two_float_ingestion_validation.py`
**Architecture validated:** `backend/bgc_matching.py`, `backend/data_processor.py`,
`backend/database_manager.py`, migrations V3 and V4.
**Report generated from 4679 executed checks** (0 failed).

---

## 1. Environment and database isolation

| Item | Value |
|---|---|
| MySQL server | 8.0.46 |
| Host / port | localhost:3306 |
| User | `root` |
| Development database (read-only fingerprint) | `argo_ocean_data_fresh` |
| Disposable validation database | `intelliquery_bgc_two_float_check` |
| Dataset root (read-only) | `C:\AI Projects\IntelliQuery\backend\bgc data\bgc raw data` |
| Schema source | `create_mysql_tables()` + V2, V3, V4 applied in order |

The disposable database is dropped at the end unless `--keep-db` is given. The
development database is opened only for `SELECT`/`SHOW` statements: it is
fingerprinted before and after, never written. Ingestion reaches the disposable
database through the production `ArgoDataProcessor` / `DatabaseManager` code,
with only the ChromaDB write replaced by a recorder so no vector store is
touched.

Development database identity before the run: **1384 profiles**,
**22 floats**, schema digest over
39 columns and
7 index entries.

## 2. Database identity and safety verification

| Check | Result |
|---|---|
| Disposable name differs from `DB_NAME` | pass |
| Base schema + V2 + V3 + V4 applied | 7 statements |
| `direction`, `bgc_pressure`, `bgc_source_metadata` present | pass |
| `UNIQUE (float_id, cycle_number, direction, profile_time)` present | pass |
| Narrower `(float_id, cycle_number, profile_index_in_file)` key gone | pass |
| `argo_floats` foreign key intact | pass |
| Corpus fingerprint before ingestion | 1264 files, size+mtime recorded |
| Files used by this validation, SHA-256 recorded | 406 files |

## 3. Float 3902581 ingestion results

ascending and descending profiles of one cycle. First pass against the empty disposable database:

| Measure | Value |
|---|---|
| Source files | 236 |
| `(float, cycle)` groups | 112 |
| Inserted | 118 |
| Skipped | 0 |
| Errors | 0 |
| Wall clock | 466.0 s |
| Stored rows | 118 |
| Direction distribution | {'A': 112, 'D': 6} |
| Selected core `DATA_MODE` | {'R': 67, 'D': 51} |
| Core variants per profile | {4: 112, 3: 6} |

All 118 rows carry a BGC product. The 13 profiles whose delayed-mode
core variant holds less than 90 % of the maximum level count are stored with the
`most_levels` variant and list the delayed variant in
`source_metadata.core_variants`, exactly as the audit predicted.

## 4. Float 2902306 ingestion results

10 of the 13 pres_scale_suspect grids. Second pass, on the same disposable database:

| Measure | Value |
|---|---|
| Source files | 170 |
| `(float, cycle)` groups | 85 |
| Inserted | 85 |
| Skipped | 0 |
| Errors | 0 |
| Wall clock | 351.4 s |
| Stored rows | 85 |
| Direction distribution | {'A': 85} |
| Selected core `DATA_MODE` | {'D': 37, 'A': 47, 'R': 1} |
| Core variants per profile | {1: 10, 2: 75} |

## 5. Expected versus actual profile counts

| Float | Audit logical profiles | Planned identities | Inserted (first pass) | Rows after | Difference |
|---|---|---|---|---|---|
| 3902581 | 118 | 118 | 118 | 118 | 0 |
| 2902306 | 85 | 85 | 85 | 85 | 0 |
| Total | 203 | 203 | 203 | 203 | 0 |

"Planned identities" is the production matcher's own plan for the same files,
recomputed from the sources, and compared with the stored identities in section 6.

## 6. Source-to-database validation results

Every stored row was matched back to its selected core NetCDF `N_PROF` and to
its BGC `N_PROF`, and every stored value was compared with the source array
(numerical tolerance 1e-6, `None` for fill values and NaNs preserved; 1e-4
degrees for the single-precision `FLOAT` position columns).

| Field group | Float 3902581 (values checked) | Float 2902306 (values checked) |
|---|---|---|
| `PRES`, `TEMP`, `PSAL` raw | 354/354 | 255/255 |
| Measurement `*_QC` flags | 354/354 | 255/255 |
| `*_ADJUSTED` values | 354/354 | 255/255 |
| `*_ADJUSTED_ERROR` values | 354/354 | 255/255 |
| `*_ADJUSTED_QC` flags | 354/354 | 255/255 |
| `DATA_MODE` and `PROFILE_*_QC` | 472/472 | 340/340 |
| Latitude / longitude | 236/236 | 170/170 |
| `source_metadata` fields | 590/590 | 425/425 |
| `source_metadata.core_variants` | 118/118 | 85/85 |
| `bgc_pressure` axis | 118/118 | 85/85 |
| BGC parameter set | 118/118 | 85/85 |
| BGC raw / QC / adjusted / error / adjusted QC | 590/590 | 425/425 |
| BGC array lengths equal the axis | 2360/2360 | 1895/1895 |
| `parameter_data_mode` | 118/118 | 85/85 |

The counts are stored-value comparisons against the source arrays, one per
profile and field. The per-row identity, provenance, variant-selection and BGC
metadata assertions are separate checks (see the section totals in section 14);
the generated JSON (`scratch/bgc_validation/summary.json`) holds every count and
mismatch example.

## 7. Parameter coverage validation

Only parameters delivered by the source are stored; nothing is invented.

| Float | Parameter | Profiles with the parameter |
|---|---|---|
| 3902581 | DOXY | 118 / 118 |
| 3902581 | CHLA | 118 / 118 |
| 3902581 | BBP700 | 118 / 118 |
| 2902306 | DOXY | 75 / 85 |
| 2902306 | CHLA | 75 / 85 |
| 2902306 | BBP700 | 75 / 85 |
| 2902306 | NITRATE | 69 / 85 |
| 2902306 | PH_IN_SITU_TOTAL | 85 / 85 |

## 8. Pressure-grid anomaly handling

| Float | Grid status | Profiles |
|---|---|---|
| 3902581 | {'ok': 118} | 118 |
| 2902306 | {'pres_scale_suspect': 10, 'ok': 75} | 85 |

The ten known `pres_scale_suspect` profiles of float 2902306:

| Cycle | Dir | BGC file | Core max (dbar) | BGC max (dbar) | Median ratio | BGC levels |
|---|---|---|---|---|---|---|
| 1 | A | `SD2902306_001.nc` | 1995.77 | 1.996 | 1000.01 | 996 |
| 11 | A | `SD2902306_011.nc` | 1995.95 | 1.996 | 998.981 | 996 |
| 16 | A | `SD2902306_016.nc` | 1997.27 | 1.997 | 1000.005 | 998 |
| 17 | A | `SD2902306_017.nc` | 1995.87 | 1.996 | 1000.0 | 997 |
| 18 | A | `SD2902306_018.nc` | 1995.95 | 1.996 | 999.99 | 997 |
| 23 | A | `SD2902306_023.nc` | 1997.3 | 1.997 | 1000.005 | 998 |
| 24 | A | `SD2902306_024.nc` | 1995.87 | 1.996 | 1000.02 | 997 |
| 25 | A | `SD2902306_025.nc` | 1995.4399 | 1.995 | 999.031 | 996 |
| 26 | A | `SD2902306_026.nc` | 1995.89 | 1.996 | 1000.0 | 997 |
| 39 | A | `SR2902306_039.nc` | 1997.3 | 1.997 | 1000.02 | 998 |

* Nothing was rescaled: the stored `bgc_pressure` equals the BGC file's `PRES`
  array element for element, and the core `pressure` equals the core file's.
* BGC measurements of the anomalous profiles are preserved in
  `bgc_params` / `bgc_adjusted` and match the source.
* The flag is persisted in `bgc_source_metadata.grid_check` and is directly
  queryable (`JSON_EXTRACT(bgc_source_metadata, '$.grid_check')`); a BGC-versus-
  depth consumer can exclude those rows without reading the pressure axis.
  The current API layer does not yet expose BGC arrays or this flag (the Data
  Explorer work is out of scope), so no existing consumer treats them as valid.
* The remaining 75 profiles of float 2902306
  are `ok`.

## 9. Idempotency results

Both floats were re-ingested with the exact same file list immediately after
the first pass.

| Measure | Value |
|---|---|
| Second-pass inserted | 0 |
| Second-pass skipped | 203 |
| Second-pass errors | 0 |
| Rows before / after | 203 / 203 |
| Duplicate identities after re-run | 0 |
| Payload digests changed | 0 |
| `ingestion_timestamp` rewrites | 0 |

The digest covers every stored column of every row, so an unchanged digest after
the second pass means no measurement, no metadata field and no provenance value
was rewritten. Re-ingestion is a no-op by design: the existence check runs on
the logical identity and the unique key is the backstop.

## 10. Integrity checks

| Check | Result |
|---|---|
| Duplicate logical identities | 0 |
| Rows per float equal the audit | 118 / 118 and 85 / 85 |
| Identities equal the ingestion plan (missing / unexpected) | missing 0, unexpected 0 |
| Every merged row has a BGC product and a separate BGC source file | pass |
| JSON array lengths consistent inside each row (core and BGC) | pass |
| `direction` always `A`/`D`, `profile_time` never NULL | pass |
| Only the two expected floats were created | 2 float rows |
| Unique-key rejection of a duplicate identity (rolled back) | confirmed |
| Rollback leaves the row count unchanged | pass |
| Development database untouched (rows, payload sums, schema) | yes |

Transaction behaviour: each row is committed individually by
`DatabaseManager.insert_profile`. A crash mid-run therefore keeps the rows
already written, and the same run repeated skips them; it cannot leave a
partially written row, because one insert writes one row. This was confirmed by
reading the write path and by the idempotency pass, not by killing a process.

## 11. Development database unchanged verification

| Measure | Before | After |
|---|---|---|
| `argo_profiles` rows | 1384 | 1384 |
| `argo_floats` rows | 22 | 22 |
| Payload sums (identity hash, PRES, TEMP, PSAL, BGC lengths) | {'rows_total': 1384, 'identity_hash': Decimal('3050654758879'), 'pres': Decimal('1353528'), 'temp': Decimal('1569851'), 'psal': Decimal('1550557'), 'bgc': Decimal('0')} | {'rows_total': 1384, 'identity_hash': Decimal('3050654758879'), 'pres': Decimal('1353528'), 'temp': Decimal('1569851'), 'psal': Decimal('1550557'), 'bgc': Decimal('0')} |
| Schema (tables, columns, indexes) | unchanged | unchanged |

The corpus fingerprint also matched: 1264 files with identical
size and mtime, and 406 SHA-256 hashes of every file used in
this validation unchanged. The raw NetCDF files were only opened for reading.

## 12. Defects discovered and fixes applied

**None.** The writing pipeline required no change: every stored row matched its source file, the two floats stored exactly the planned profiles, and re-ingestion was a no-op. No production file was modified for this validation.

Three defects in the *validator itself* were found and corrected while building
it, before this final run: 0-D object arrays that hold a complete bytes value
were decoded as the string `b'...'`; the single-precision `FLOAT` position
columns were compared at 1e-6 instead of their 1e-4 resolution; and
`level_count` was compared with the audit's netCDF-masked level count although
it stores the delivered axis length. None of them changed production code.

## 13. Test results

Command: `python -m unittest discover -p "test_*.py"` from `backend/`.

`Ran 754 tests in 148.161s`

`OK (skipped=9)`

## 14. Checks and final verdict

**4679 checks executed, 4679 passed,
0 failed.**

| Section | Passed | Failed |
|---|---|---|
| Setup: corpus and development database fingerprints | 3 | 0 |
| Phase 1: disposable database and V4 schema | 6 | 0 |
| Phase 2: float 3902581 ingestion | 4 | 0 |
| Phase 3: float 3902581 stored rows against the sources | 2658 | 0 |
| Phase 4: float 2902306 ingestion and grid anomalies | 1983 | 0 |
| Phase 5: idempotency | 9 | 0 |
| Phase 6: database integrity | 8 | 0 |
| Phase 7: development database and corpus unchanged | 7 | 0 |
| Unit tests | 1 | 0 |

Failed checks:

None.


### Verdict

**PASS** — both floats were stored and verified against their source files, re-ingestion inserted nothing, no identity or payload changed, and the development database and corpus are untouched.
