# IntelliQuery — BGC Validation Correction & Evidence Reconciliation Report

**Date:** 2026-11-26  
**Scope:** Resolve and document the two validation exceptions reported in
`docs/bgc/BGC_CHROMADB_HARDENING_REPORT.md` (71/73 checks passed), reconcile
validation evidence across artifacts, and establish whether a corrected
632-row promotion dataset exists in MySQL.

**Development Environment Status:** `argo_ocean_data_fresh` (1,384 profiles,
22 floats) and `backend/chroma_db_storage_fresh` (`argo_float_profiles`,
1,384 vectors, digest `a3e41263d9d9640e`) remain **completely untouched**.

---

## 1. Reconciliation of Full-Corpus Hardening Result (71/73) vs Targeted Corrections

The full-corpus run reported in `docs/bgc/BGC_CHROMADB_HARDENING_REPORT.md`
(`python scripts/bgc_chromadb_hardening_validation.py --full-corpus`) executed
**73 checks**, of which **71 passed and 2 failed**:

| Run / Scope | Result | Notes |
|---|---|---|
| Hardening representative phase (`bgc_chromadb_hardening_validation.py`) | **55 / 55 passed** | Exercised crash-window recovery, idempotency, retry, metadata filters, and core retrieval on 11 representative profiles. |
| Hardening full-corpus phase (`--full-corpus`) | **71 / 73 passed (2 failed)** | Original run did **not** pass 73/73 and is preserved as 71/73. Both failures trace to 2 of the 11 pre-seeded representative profiles coming from partial cycle file subsets. |
| Targeted complete-group correction check (`bgc_exception_check`) | **2 / 2 profiles verified** | Reprocessed the complete `(float, cycle)` source groups (4 NetCDF files) for `6990679` cycle `1` and `7902200` cycle `15` in a fresh disposable DB and ChromaDB store. |

### Why the 2 Full-Corpus Hardening Checks Failed

In `scripts/bgc_chromadb_hardening_validation.py`, the 11-profile
representative set (`base.INGEST_FILES`) is ingested first, followed by the
1,264-file corpus in the same disposable database. Because `ArgoDataProcessor`
skips existing logical identities `(float_id, cycle_number, direction, profile_time)`
without overwriting or re-planning them, the two profiles pre-seeded from
partial file subsets retained their partial-subset plans:

1. **`6990679` / cycle `1` (`A`)**: Pre-seeded from `R6990679_001.nc` alone
   (`core_only`), so the later full-corpus pass skipped the identity instead of
   attaching `SR6990679_001.nc`. Result in `scratch/bgc_hardening/chroma_store`
   (`profile_8`): `product_type='core'`, `has_bgc=False`, `bgc_parameters=''`,
   reducing `DOXY`/`CHLA`/`BBP700` counts from 619 to 618.
2. **`7902200` / cycle `15` (`A`)**: Pre-seeded from `SR7902200_015.nc` alone
   (`bgc_only`), so `classify_pressure_grid` ran with `core_pressure=None` and
   recorded `grid_check='ok'` instead of comparing against `R7902200_015.nc`.
   Result in `scratch/bgc_hardening/checks.json`: 12 `pres_scale_suspect` rows
   instead of 13.

This report does **not** alter the historical 71/73 record in
`BGC_CHROMADB_HARDENING_REPORT.md`; instead, it pairs that baseline with
complete-group verification of both exception identities.

---

## 2. Investigation and Verification of the Two Exception Profiles

All `.nc` files under `backend/bgc data/bgc raw data` matching floats `6990679`
and `7902200` were indexed with `bgc_matching.index_file`, grouped with
`group_by_cycle`, and planned with `plan_group`. No additional core or BGC files
exist in the corpus for either `(float, cycle)` group.

Both complete groups were ingested through `ArgoDataProcessor.process_paths`
into a disposable MySQL database (`intelliquery_bgc_exception_check`, dropped
after verification) and an isolated ChromaDB store
(`backend/scratch/bgc_exception_check/chroma_store`, collection
`argo_float_profiles_exception_check`). Evidence is saved in
`backend/scratch/bgc_exception_check/evidence.json`.

### Exception 1: Float `6990679`, Cycle `1`

| Property | Partial-Subset Result (71/73 Run) | Complete-Group Result (Verified) |
|---|---|---|
| Source files in group | `R6990679_001.nc` only | `R6990679_001.nc` (4 `N_PROF` variants) + `SR6990679_001.nc` (1 `N_PROF`) |
| Logical identity | `(6990679, 1, 'A', 2024-12-01 13:28:00)` | `(6990679, 1, 'A', 2024-12-01 13:28:00)` |
| Match status | `core_only` | `matched` |
| Selected core variant | `N_PROF #3` (93 levels, `most_levels`) | `N_PROF #3` (93 levels, `most_levels`, 4 variants in `core_variants`) |
| BGC axis (`bgc_pressure`) | `NULL` | 186 levels (`0.5` – `1964.8101` dbar), exact match to `SR6990679_001.nc` |
| BGC parameters stored | None (`NULL`) | `BBP700`, `CHLA`, `CHLA_FLUORESCENCE`, `DOXY` (contains `DOXY`, `CHLA`, `BBP700`) |
| `grid_check` | `NULL` | `ok` (`median_ratio=1.0`, `overlap_fraction=1.0`) |
| ChromaDB metadata | `product_type='core'`, `has_bgc=False`, `bgc_parameters=''` | `product_type='core_bgc'`, `has_bgc=True`, `bgc_parameters='BBP700,CHLA,CHLA_FLUORESCENCE,DOXY'`, `bgc_parameter_count=4`, `direction='A'` |

### Exception 2: Float `7902200`, Cycle `15`

| Property | Partial-Subset Result (71/73 Run) | Complete-Group Result (Verified) |
|---|---|---|
| Source files in group | `SR7902200_015.nc` only | `R7902200_015.nc` (1 `N_PROF`, 997 levels) + `SR7902200_015.nc` (1 `N_PROF`, 997 levels) |
| Logical identity | `(7902200, 15, 'A', 2024-12-26 13:10:58)` | `(7902200, 15, 'A', 2024-12-26 13:10:58)` |
| Match status | `bgc_only` | `matched` |
| Core axis (`pressure`) |Copied from BGC (`0.004` – `1.996` dbar) | `4.2` – `1995.8199` dbar (997 levels, exact match to `R7902200_015.nc`) |
| BGC axis (`bgc_pressure`) | `0.004` – `1.996` dbar | `0.004` – `1.996` dbar (997 levels, preserved raw without rescaling) |
| BGC parameters stored | `PH_IN_SITU_TOTAL` | `PH_IN_SITU_TOTAL` |
| `grid_check` | `ok` (no core counterpart) | `pres_scale_suspect` (`median_ratio=999.95`) |
| ChromaDB metadata | `product_type='core_bgc'`, `has_bgc=True`, `bgc_parameters='PH_IN_SITU_TOTAL'` | `product_type='core_bgc'`, `has_bgc=True`, `bgc_parameters='PH_IN_SITU_TOTAL'`, `bgc_parameter_count=1`, `direction='A'` |

### Verification Checks on Targeted Reprocessing

* **Ingestion summary:** `files=4, groups=2, inserted=2, skipped=0, errors=[]`.
* **Second-pass idempotency:** `files=4, groups=2, inserted=0, skipped=2, errors=[]`;
  `sync_profile_vector` returned `unchanged` for both profiles (`profile_1`, `profile_2`).
* **Source-to-SQL fidelity:** Core (`PRES`, `TEMP`, `PSAL`), `bgc_pressure`,
  `bgc_params`, `bgc_adjusted`, and `bgc_qc` matched the raw NetCDF arrays
  with aligned array lengths (`93` core levels / `186` BGC levels for `6990679`
  cyc `1`; `997` core / `997` BGC levels for `7902200` cyc `15`).
* **SQL-to-ChromaDB parity:** Both vectors matched `expected_metadata(row)`
  exactly, and both documents contained `profile_id` and all BGC parameter names.
* **Non-modification checks:** SHA-256 hashes of all 4 NetCDF files were
  unchanged (`netcdf_unchanged=True`), and development MySQL and ChromaDB were
  unchanged (`dev_db_unchanged=True`, `dev_chroma_unchanged=True`).

---

## 3. Code Fingerprints and Timing Evidence (Preserved)

Verified SHA-256 digests on disk match `BGC_CHROMADB_HARDENING_REPORT.md`
(production ingestion and matching modules were not modified in this phase):

| File | SHA-256 |
|---|---|
| `backend/data_processor.py` | `ec6da436d3a9fdcd7e2510281600635f0c773b92774ababe` |
| `backend/database_manager.py` | `7de9f13eb8bf6b70287f177df97765f34ee2d61f61b2e55c4312ce51a51d60fe` |
| `backend/bgc_matching.py` | `fbad867697e5802b49903f69ff7326f00f5b1b2c156bad89aa11e3092d37d059` |
| `backend/scripts/bgc_chromadb_hardening_validation.py` | `534407b1e83b7f1881d80e507c056be8ecb3fab637afe39fe0bf32aa577feeea` |

Preserved timing evidence from prior full-corpus runs:
* `bgc_chromadb_hardening_validation.py --full-corpus`: **2,638.7 s** corpus
  ingestion (621 inserted + 11 skipped), **2,864.4 s** total run time.
* `bgc_full_corpus_ingestion_validation.py`: **3,707.4 s** for 547 profiles
  across the 7 uninterrupted floats (~6.8 s/profile; float `2902306` and the
  second pass included host sleep periods).

---

## 4. Inventory of Validation Artifacts & Promotion Source Availability

### Available Artifacts on Disk

* **Source corpus (authoritative):** `backend/bgc data/bgc raw data` — 1,264
  `.nc` files (257.1 MB), 626 `(float, cycle)` groups, 632 planned logical profiles.
* **Dry-run audit artifacts:** `backend/scratch/bgc_audit/{index.json, plans.json, summary.json}`.
* **Full-corpus validation artifacts:** `backend/scratch/bgc_full_validation/{summary.json, run.log, chroma_store/}`.
  (`chroma_store` holds 632 pre-hardening vectors without `has_bgc`/`product_type` metadata.)
* **Hardening validation artifacts:** `backend/scratch/bgc_hardening/{checks.json, run.log, chroma_store/, group_check_store/}`.
  (`chroma_store` holds 632 vectors from the 71/73 run, where `profile_8` and `profile_11` reflect the partial-subset pre-seed.)
* **Targeted correction artifacts (new):** `backend/scratch/bgc_exception_check/{evidence.json, chroma_store/}`.
* **Promotion dry-run report (new):** `backend/scratch/bgc_promotion/dry_run_report.json`.

### Unavailable Artifacts & Promotion Source Status

* **No relational validation MySQL database remains on the MySQL server.**
  `SHOW DATABASES` confirms zero `intelliquery_bgc%` or `intelliquery_v4%`
  databases exist (`intelliquery_bgc_full_corpus_check` and
  `intelliquery_bgc_hardening_check` were dropped at the end of their runs).
* **Consequence for promotion:** There is **no corrected 632-row MySQL dataset
  available to copy/export directly**. The canonical promotion source is
  therefore the verified **1,264-file NetCDF corpus** (`backend/bgc data/bgc raw data`)
  re-verified by `backend/scripts/bgc_promote_to_dev.py` in dry-run mode (all
  632 planned profiles `matched`, `DOXY/CHLA/BBP700 = 619`, `NITRATE = 126`,
  `PH_IN_SITU_TOTAL = 316`, `CHLA_FLUORESCENCE = 316`, `13 pres_scale_suspect`,
  `619 ok`, `0` collisions with development).

---

## 5. Automated Regression Verification

* `python -m unittest test_bgc_promotion_and_correction -v` exercises both
  exception groups (`6990679` cycle `1` and `7902200` cycle `15`) directly
  against the real NetCDF files plus promotion dry-run safety checks.
