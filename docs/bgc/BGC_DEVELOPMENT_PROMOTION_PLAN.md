# IntelliQuery — BGC Development Promotion Plan (Dry-Run Verified)

**Date:** 2026-11-26  
**Target Database:** `argo_ocean_data_fresh` (MySQL 8.0.46 on `localhost:3306`)  
**Target ChromaDB:** `backend/chroma_db_storage_fresh` (collection `argo_float_profiles`)  
**Promotion Tool:** `backend/scripts/bgc_promote_to_dev.py` (defaults to `--dry-run`; requires `--apply` with verified `--mysql-backup` and `--chroma-backup`)  
**Dry-Run Evidence:** `backend/scratch/bgc_promotion/dry_run_report.json` (**15/15 checks passed**)

> [!IMPORTANT]
> **Dry-Run Only:** No writes have been applied to `argo_ocean_data_fresh` or
> `backend/chroma_db_storage_fresh`. Execution with `--apply` is gated on
> explicit user approval and verified pre-promotion backups.

---

## 1. Target Development Schema Verification

Inspected `argo_ocean_data_fresh` live (read-only):

* **Columns:** `39` columns on `argo_profiles` — all `38`
  `DatabaseManager.REQUIRED_PROFILE_COLUMNS` plus `profile_id` are present
  (base + V2 + V3 + V4 migrations already applied).
* **Unique Keys:** `PRIMARY (profile_id)` and `uq_argo_profiles_logical
  (float_id, cycle_number, direction, profile_time)`. No narrower unique key
  remains.
* **Foreign Key:** `argo_profiles_ibfk_1` (`float_id -> argo_floats(float_id)`).

---

## 2. Current Development Baseline

| Store | Metric | Measured Value |
|---|---|---|
| MySQL (`argo_ocean_data_fresh`) | `argo_floats` rows | `22` (`max_float_id=22`, WMOs `1900121`–`2900256`) |
| MySQL (`argo_ocean_data_fresh`) | `argo_profiles` rows | `1,384` (`max_profile_id=1384`, `direction='A'` for all 1,384, `bgc_params IS NOT NULL`: `0`) |
| MySQL (`argo_ocean_data_fresh`) | SQL fingerprint | `identity_hash=2951070014956`, `pres=1353528`, `temp=1569851`, `psal=1550557`, `bgc=0` |
| ChromaDB (`argo_float_profiles`) | Vectors / SQL ID parity | `1,384` vectors (`0` missing, `0` extra) |
| ChromaDB (`argo_float_profiles`) | Vector store size | `6` files, `37,202,002` bytes |
| ChromaDB (`argo_float_profiles`) | Vector metadata status | `0` up-to-date, **`1,384` legacy vectors needing backfill** |

---

## 3. Promotion Source & Transfer Mechanism

Because the disposable validation MySQL databases
(`intelliquery_bgc_full_corpus_check`, `intelliquery_bgc_hardening_check`) were
dropped after their runs, **no 632-row validation database exists for SQL export
or `INSERT ... SELECT` copy**.

Instead, `backend/scripts/bgc_promote_to_dev.py` promotes the **632 validated
BGC profiles directly from the verified 1,264-file NetCDF corpus**
(`backend/bgc data/bgc raw data`) grouped by `(float, cycle)` through the
production `ArgoDataProcessor.process_paths` and `DatabaseManager` pipeline:

* Every `(float, cycle)` group is planned together (`plan_group`), guaranteeing
  that `6990679` cycle `1` and `7902200` cycle `15` are ingested as complete
  `matched` groups rather than partial subsets.
* Existing development rows are **never overwritten**: `_store_payload` checks
  `get_profile_id(float_id, cycle_number, direction, profile_time)` first and
  skips existing identities without touching their measurements.
* Only the **632 new BGC profiles** are inserted (`1,384` existing core rows
  remain untouched; projected total = `2,016` profiles).

---

## 4. Dry-Run Identity Collision & Foreign-Key Results

All 632 planned logical identities `(wmo_number, cycle_number, direction, profile_time)`
were cross-checked against `argo_ocean_data_fresh`:

| Check | Dry-Run Result |
|---|---|
| Corpus files scanned | `1,264` (`0` index errors, `0` unusable records) |
| `(float, cycle)` groups | `626` |
| Planned logical profiles | `632` (`626` ascending `'A'`, `6` descending `'D'`, `0` duplicate identities) |
| Match classification | `632` `matched` (`0` `core_only`, `0` `bgc_only`) |
| Exact logical identity collisions with dev DB | **`0`** |
| `(wmo_number, cycle_number)` overlaps with dev DB | **`0`** |
| Existing corpus WMOs already in `argo_floats` | `0` |
| New `argo_floats` records required (FK parent rows) | **`8`**: `2902306`, `2903951`, `3902581`, `4903783`, `6990679`, `7902170`, `7902190`, `7902200` |
| Eligible profiles to insert | **`632`** |
| Existing profiles preserved | `1,384` |
| Projected totals after Stage A | **`30` floats**, **`2,016` profiles** |

`get_or_create_float(wmo_number, project_name, platform_type)` creates each of
the 8 new `argo_floats` rows idempotently before inserting any child
`argo_profiles` rows for that float, satisfying `argo_profiles_ibfk_1`.

---

## 5. Measurements, Provenance, Pressure Arrays & Anomaly Flags Verified

Dry-run verification of the 632 planned profiles against the raw NetCDF corpus
confirmed:

* **Parameter coverage:** `DOXY`: `619`, `CHLA`: `619`, `BBP700`: `619`,
  `NITRATE`: `126`, `PH_IN_SITU_TOTAL`: `316`, `CHLA_FLUORESCENCE`: `316`.
* **Pressure-grid audit:** `619` `ok`, **`13` `pres_scale_suspect`** (`2902306`
  cycles `1, 11, 16, 17, 18, 23, 24, 25, 26, 39`; `7902170` cycles `22, 26`;
  `7902200` cycle `15`), `0` `missing_pressure`, `0` `grid_mismatch`.
* **Scientific integrity:** BGC measurements remain on their own `bgc_pressure`
  axis; rescaled synthetic pressure grids (~1000x smaller than core `PRES`)
  are preserved raw and flagged in `bgc_source_metadata.grid_check`.

---

## 6. Two-Stage Execution Design

Promotion is split into two independently executable stages:

### Stage A — BGC Ingestion (`--apply --stage ingest`)
* Verifies target DB/collection names, unmodified baseline fingerprint, and
  real backup artifacts (`--mysql-backup`, `--chroma-backup`).
* Ingests the 632 complete logical profiles via `ArgoDataProcessor.process_paths`.
* Creates vectors for newly committed rows via `add_profile_from_row`.
* Preserves all `1,384` existing development rows; reconciles any missing
  vector for previously committed BGC rows when resumed with `--resume`.
* Verifies post-Stage-A parity: **`2,016` SQL rows**, **`30` floats**,
  `0` duplicate identities, and **`2,016` ChromaDB vectors** (`0` missing, `0` extra).
* Writes `backend/scratch/bgc_promotion/stage_a_manifest.json` recording:
  * `target_database` and `target_chroma_collection` (safety verification keys).
  * `promoted_wmos` — the 8 new WMO numbers.
  * `inserted_floats` — list of `{wmo_number, float_id}` pairs for each newly created `argo_floats` row.
  * `inserted_profiles` — full logical identity per newly committed row: `profile_id`, `float_id`, `wmo_number`, `cycle_number`, `direction`, `profile_time`.
  * `inserted_vector_ids` — ChromaDB vector IDs (`profile_<id>`) for each new profile.

### Stage B — Legacy Vector Backfill (`--apply --stage backfill`)
* Verifies target DB/collection names and real backup artifacts.
* Runs `DatabaseManager.sync_profile_vector(profile_id)` across all stored
  profiles (`ORDER BY profile_id`).
* Reports **`updated`**, **`unchanged`**, **`recreated`**, **`missing_before/after`**,
  and **`failed`** (with per-profile errors) separately.
* **Re-embedding behavior:** Because all `1,384` legacy vectors differ from
  the current `_build_profile_vector_payload` output in both metadata (missing
  `direction`, `has_bgc`, `product_type`, `bgc_parameters`, `bgc_parameter_count`)
  and document text (missing `Profile Direction:` and `Bio-geochemical Parameters`
  sections, fractional timestamps), `sync_profile_vector` calls
  `collection.update` with `self._embed_document(document)` — **re-embedding
  all 1,384 legacy documents** (~1–3 minutes locally with `all-MiniLM-L6-v2`).
* Any already-current vectors (e.g., the 632 vectors created in Stage A, or
  vectors from a partial prior Stage B run) return `unchanged` with **zero
  re-embedding**.

---

## 7. Backup Verification, Rollback, Resumability & Idempotency

All PowerShell commands below assume the working directory is **`backend/`**:

### Pre-Promotion Backup (Mandatory before `--apply`)

1. **MySQL backup** (run from `backend/`):
   ```powershell
   mysqldump -u <DB_USER> -p --single-transaction --routines --triggers argo_ocean_data_fresh > backups\argo_ocean_data_fresh_pre_bgc_promotion.sql
   ```
2. **ChromaDB backup** (run from `backend/` while the backend server is stopped):
   ```powershell
   Copy-Item -Recurse -Path chroma_db_storage_fresh -Destination backups\chroma_db_storage_fresh_pre_bgc_promotion
   ```

Before any mutation, `bgc_promote_to_dev.py --apply` programmatically verifies:
* `--mysql-backup` exists, is a regular file, and has `size > 0` bytes.
* `--chroma-backup` exists, is not the live `CHROMA_PERSIST_DIR`, contains
  non-empty `chroma.sqlite3`, and contains collection `argo_float_profiles`
  with at least the expected baseline vector count (`1,384`).
* `DB_NAME == 'argo_ocean_data_fresh'` and `CHROMA_COLLECTION_NAME == 'argo_float_profiles'`.
* On fresh Stage A, live SQL fingerprint and vector count match `dry_run_report.json`.

### Targeted Rollback (Preferred) vs Emergency Full Restore

* **Targeted ownership-verified rollback of promoted Stage A records (preferred):**

  `bgc_promote_to_dev.py --rollback-stage-a` reads
  `scratch/bgc_promotion/stage_a_manifest.json` and performs record-by-record
  ownership checks **before** deleting anything:

  1. Verifies `target_database` and `target_chroma_collection` in the manifest
     match the runtime environment.
  2. Verifies every recorded `profile_id` still has the exact `(wmo_number,
     cycle_number, direction, profile_time)` from `inserted_profiles`.
  3. Verifies no unexpected child profiles reference a promoted float row.
  4. Verifies each ChromaDB vector's `profile_id_sql` metadata matches the manifest.
  5. **Refuses** with a descriptive error if any mismatch or unexpected record is detected.
  6. If all checks pass, deletes only the recorded vector IDs from ChromaDB,
     deletes only the recorded `argo_profiles` rows in a single MySQL transaction,
     then deletes each promoted `argo_floats` row only if no remaining profiles
     reference it.

  ```powershell
  # Dry-run rollback (verify ownership without deleting):
  python scripts/bgc_promote_to_dev.py --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles

  # Applied rollback (deletes only manifest-owned records):
  python scripts/bgc_promote_to_dev.py --apply --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles
  ```

  After a successful applied rollback, verify: `argo_profiles = 1,384`,
  `argo_floats = 22`, ChromaDB vectors = `1,384`.

* **Emergency full restore (if Stage B or broader state must be reverted):**
  Run from `backend/`:
  ```powershell
  mysql -u <DB_USER> -p argo_ocean_data_fresh < backups\argo_ocean_data_fresh_pre_bgc_promotion.sql
  Remove-Item -Recurse -Force chroma_db_storage_fresh
  Copy-Item -Recurse -Path backups\chroma_db_storage_fresh_pre_bgc_promotion -Destination chroma_db_storage_fresh
  ```

### Resumability & Idempotency

* **Interrupted Stage A:** Re-run with `--apply --stage ingest --resume` (plus
  the backup flags). Already-committed rows are skipped without overwriting
  measurements, and any row committed without its vector is repaired via
  `sync_profile_vector`.
* **Interrupted Stage B:** Re-run `--apply --stage backfill` (plus the backup
  flags). Already-updated vectors return `unchanged`; remaining or previously
  failed vectors are retried.

---

## 8. Execution Commands (Awaiting Explicit Approval)

Run from `backend/`:

* **Dry-run (safe, default):**
  ```powershell
  python scripts/bgc_promote_to_dev.py
  ```
* **Stage A — BGC Ingestion (DO NOT RUN without approval & backups):**
  ```powershell
  python scripts/bgc_promote_to_dev.py --apply --stage ingest --mysql-backup backups\argo_ocean_data_fresh_pre_bgc_promotion.sql --chroma-backup backups\chroma_db_storage_fresh_pre_bgc_promotion
  ```
* **Stage B — Legacy Vector Backfill (DO NOT RUN without approval & backups):**
  ```powershell
  python scripts/bgc_promote_to_dev.py --apply --stage backfill --mysql-backup backups\argo_ocean_data_fresh_pre_bgc_promotion.sql --chroma-backup backups\chroma_db_storage_fresh_pre_bgc_promotion
  ```
* **Targeted rollback — Dry-run (verify ownership, no deletes):**
  ```powershell
  python scripts/bgc_promote_to_dev.py --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles
  ```
* **Targeted rollback — Applied (deletes only manifest-owned records):**
  ```powershell
  python scripts/bgc_promote_to_dev.py --apply --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles
  ```
