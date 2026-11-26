# IntelliQuery — BGC Promotion Stage A Execution Report

**Execution Date & Time:** 2026-11-26 (Started ~14:16 IST; Completed `2026-11-26T09:35:00.185621+00:00` / 15:05 IST)  
**Stage Executed:** **Stage A — BGC Ingestion Only** (`--apply --stage ingest`)  
**Stage B Status:** **NOT EXECUTED** (Legacy ChromaDB metadata backfill for the 1,384 existing vectors remains unexecuted, awaiting separate approval)  
**Target MySQL Database:** `argo_ocean_data_fresh` (`localhost:3306`)  
**Target ChromaDB Store & Collection:** `backend/chroma_db_storage_fresh` / `argo_float_profiles`  
**Manifest Location:** `backend/scratch/bgc_promotion/stage_a_manifest.json`

---

## 1. Pre-Execution Checks & Dry-Run Results

Before any mutation, `backend/scripts/bgc_promote_to_dev.py` was run in default dry-run mode from `backend/`:

* **Target verification:** `DB_NAME=argo_ocean_data_fresh`, `CHROMA_PERSIST_DIR=chroma_db_storage_fresh`, `CHROMA_COLLECTION_NAME=argo_float_profiles`.
* **Schema V4 readiness:** `True` (`39` columns on `argo_profiles`).
* **Baseline before execution:** `1,384` profiles, `22` floats, `1,384` ChromaDB vectors, `0` BGC profiles (`identity_hash=2951070014956`, `pres=1353528`, `temp=1569851`, `psal=1550557`, `bgc=0`).
* **Corpus plan:** `1,264` files, `626` `(float, cycle)` groups, `632` planned profiles, `0` identity collisions, `8` new floats required.
* **Dry-run checks:** **15 / 15 passed**.

---

## 2. Backup Paths & Verification Results

Both pre-promotion backups were created in `backend/backups/` and verified before running Stage A:

| Store | Backup Path | Verification Result |
|---|---|---|
| MySQL (`argo_ocean_data_fresh`) | `backend/backups/argo_ocean_data_fresh_pre_bgc_promotion.sql` | Exists, regular file, **`13,287,812` bytes** (non-empty) |
| ChromaDB (`chroma_db_storage_fresh`) | `backend/backups/chroma_db_storage_fresh_pre_bgc_promotion` | Exists, `chroma.sqlite3` **`35,471,360` bytes**, collection `argo_float_profiles` present with **`1,384` vectors** |

---

## 3. Stage A Execution Command & Outcome

Executed from `backend/`:

```powershell
python scripts/bgc_promote_to_dev.py --apply --stage ingest --mysql-backup "backups\argo_ocean_data_fresh_pre_bgc_promotion.sql" --chroma-backup "backups\chroma_db_storage_fresh_pre_bgc_promotion"
```

* **Ingestion summary (from `stage_a_manifest.json`):**
  * Files scanned: `1,264`
  * `(float, cycle)` groups processed: `626`
  * Profiles inserted: **`632`** (`profile_id` `1385`–`2016`)
  * Profiles skipped: **`0`**
  * Ingestion errors: **`0`** (`[]`)

---

## 4. Expected vs. Actual Measured Results

| Metric | Expected | Actual Measured | Status |
|---|---:|---:|---|
| Existing profiles preserved | 1,384 | 1,384 | PASS |
| Newly ingested profiles | 632 | 632 (`626` `'A'`, `6` `'D'`) | PASS |
| Total SQL profiles | 2,016 | 2,016 (`max_profile_id=2016`) | PASS |
| Existing floats preserved | 22 | 22 (`float_id` `1`–`22`) | PASS |
| Newly added floats | 8 | 8 (`float_id` `23`–`30`) | PASS |
| Total SQL floats | 30 | 30 (`max_float_id=30`) | PASS |
| Total ChromaDB vectors | 2,016 | 2,016 (`profile_1`–`profile_2016`) | PASS |
| Duplicate logical identities | 0 | 0 | PASS |
| SQL / ChromaDB parity errors | 0 | 0 | PASS |
| Ingestion errors | 0 | 0 | PASS |

---

## 5. Detailed Verification Checks

### Float Counts & New WMO Numbers
All 8 expected BGC WMO numbers were created idempotently and linked via `float_id`:

* `2902306` (`float_id=23`)
* `2903951` (`float_id=24`)
* `3902581` (`float_id=25`)
* `4903783` (`float_id=26`)
* `6990679` (`float_id=27`)
* `7902170` (`float_id=28`)
* `7902190` (`float_id=29`)
* `7902200` (`float_id=30`)

### SQL / ChromaDB Parity & Vector Metadata
Independent cross-store verification across all `2,016` rows in `argo_profiles` and all `2,016` vectors in `argo_float_profiles`:

* `0` missing vectors, `0` extra vectors.
* `0` mismatches on `profile_id_sql`, `float_id_sql` (WMO number), or `cycle_number`.
* All `632` newly created vectors (`profile_1385`–`profile_2016`) carry `has_bgc=True`, `product_type='core_bgc'`, and matching `direction`.

### Duplicate Identity Check
* `SELECT ... GROUP BY float_id, cycle_number, direction, profile_time HAVING COUNT(*) > 1` returned **`0`** duplicate logical identities.

### Pressure Anomaly Handling & Exception Profiles
* **`13` `pres_scale_suspect` profiles** remain flagged in `bgc_source_metadata.grid_check` with raw measurements preserved (not rescaled or altered).
* **`6990679` cycle `1`** (`profile_id=1760`, `direction='A'`): ingested with `['BBP700', 'CHLA', 'CHLA_FLUORESCENCE', 'DOXY']` and `grid_check='ok'`.
* **`7902200` cycle `15`** (`profile_id=1948`, `direction='A'`): ingested with `['PH_IN_SITU_TOTAL']` and `grid_check='pres_scale_suspect'`.

### Preservation of Original Development Stores
* **Original `1,384` development records in `argo_ocean_data_fresh`:** Unchanged. Fingerprint on `profile_id <= 1384` matches the pre-promotion baseline identically (`rows=1384`, `max_profile_id=1384`, `identity_hash=2951070014956`, `pres=1353528`, `temp=1569851`, `psal=1550557`, `bgc=0`), and all `1,384` original ChromaDB vectors (`profile_1`–`profile_1384`) remain intact.
* **Legacy `argo_ocean_data` database:** Untouched (`45` rows).
* **Raw NetCDF corpus:** Unmodified.

---

## 6. Warnings & Notes

* **Cosmetic PowerShell exit code `1` on wrapper command:** During execution, `huggingface_hub` emitted an informational stderr warning (`Warning: You are sending unauthenticated requests to the HF Hub...`) when loading the locally cached embedding model. Windows PowerShell treated that stderr line as a `NativeCommandError` (`exit code 1`) even though the Python promotion script completed cleanly, wrote `stage_a_manifest.json` with `errors: []`, and committed all `632` profiles and `632` vectors.
* **Stage B (`--stage backfill`) was NOT executed.** The `1,384` pre-existing ChromaDB vectors (`profile_1`–`profile_1384`) remain in their legacy metadata format until Stage B is explicitly approved.
