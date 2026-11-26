# IntelliQuery — Final BGC Promotion Script Safety Audit

**Date:** 2026-11-26  
**Audited Script:** `backend/scripts/bgc_promote_to_dev.py`  
**Supporting Production Modules:** `backend/data_processor.py`, `backend/database_manager.py`, `backend/bgc_matching.py`  
**Test Suite:** `backend/test_bgc_promotion_and_correction.py`  
**Verdict:** **PASS — Ready for staged promotion upon explicit user approval.**  
No promotion stage (`--apply`) was executed; `argo_ocean_data_fresh` and
`backend/chroma_db_storage_fresh` remain untouched.

---

## 1. Audit Findings & Hardening Applied

Inspecting the initial `bgc_promote_to_dev.py` and `BGC_DEVELOPMENT_PROMOTION_PLAN.md`
against production `data_processor.py` and `database_manager.py` identified five
safety gaps, all of which are now resolved:

1. **Boolean `--confirm-backup` replaced by real artifact verification (`verify_backups`):**
   * Previously `--confirm-backup` trusted a boolean flag without checking disk.
   * Now `--apply` requires `--mysql-backup <path>` and `--chroma-backup <dir>`,
     and programmatically verifies:
     * MySQL dump file exists and is non-empty (`size > 0`).
     * ChromaDB backup directory exists, is distinct from the live
       `CHROMA_PERSIST_DIR`, contains non-empty `chroma.sqlite3`, and contains
       the target collection (`argo_float_profiles`) with at least the baseline
       vector count.
2. **Target & baseline fingerprint verification (`verify_target_and_baseline`):**
   * Verifies `DB_NAME == 'argo_ocean_data_fresh'` and
     `CHROMA_COLLECTION_NAME == 'argo_float_profiles'`.
   * Compares live SQL fingerprint (`rows_total`, `floats_total`,
     `max_profile_id`, `max_float_id`, `identity_hash`, `pres`, `temp`, `psal`,
     `bgc`) and ChromaDB vector count against `dry_run_report.json` before a
     fresh Stage A run.
3. **Separated into independent Stage A (`--stage ingest`) and Stage B (`--stage backfill`):**
   * **Stage A (`run_stage_a_ingest`):** Ingests the 632 complete logical
     profiles, creates their vectors via `add_profile_from_row`, preserves
     existing rows, verifies `2,016` SQL rows, `30` floats, `0` duplicate
     identities, and `2,016` vectors (`0` missing, `0` extra), and records
     `pre_max_profile_id`/`pre_max_float_id` in
     `scratch/bgc_promotion/stage_a_manifest.json` for targeted rollback.
   * **Stage B (`run_stage_b_backfill`):** Runs `sync_profile_vector` across
     stored profiles, reporting `updated`, `unchanged`, `recreated`,
     `missing_before/after`, and `failed` (with per-profile errors) separately,
     and supporting safe resume/retry.
4. **Interrupted Stage A resume (`--resume`):**
   * Added `--resume` so an interrupted Stage A run can be re-run without
     failing the unmodified-baseline check while still skipping existing rows,
     preserving measurements, and repairing any committed row missing its vector.
5. **PowerShell working-directory consistency:**
   * Standardized all backup, restore, and promotion commands in
     `docs/bgc/BGC_DEVELOPMENT_PROMOTION_PLAN.md` to run from `backend/`.
6. **Exact promotion ownership tracking in Stage A manifest (threshold rollback eliminated):**
   * Previously, targeted rollback relied on `profile_id > 1384` / `float_id > 22` thresholds
     which cannot distinguish promotion-owned rows from concurrently inserted rows.
   * `run_stage_a_ingest` now uses pre/post ownership snapshots to record the exact sets of
     inserted profile IDs (with full logical identity), inserted float IDs (with WMO numbers),
     and inserted ChromaDB vector IDs in `scratch/bgc_promotion/stage_a_manifest.json`.
   * New `rollback_stage_a()` reads the manifest, verifies `target_database` and
     `target_chroma_collection`, performs record-by-record identity comparison, checks for
     unexpected child profiles on promoted floats, verifies ChromaDB `profile_id_sql` metadata,
     and refuses with a descriptive `RuntimeError` on any mismatch. If all checks pass,
     deletes only the recorded vectors, SQL profile rows (transactionally), and promoted float
     rows (only if unreferenced).
   * CLI flags added: `--rollback-stage-a`, `--manifest`, `--expected-db`, `--expected-collection`.

---

## 2. Failure Recovery Verification

| Failure / Edge Case | Mechanism Verified |
|---|---|
| MySQL commit atomicity | `insert_profile` commits per profile inside `with self.mysql_engine.connect()`. |
| Crash after SQL commit, before vector write | `ArgoDataProcessor._store_payload` -> `get_profile_id` + `sync_profile_vector` recreates the missing vector on Stage A `--resume` without duplicating the row. |
| Re-running ingestion | `get_profile_id` returns existing `profile_id`; `_store_payload` returns `"skipped"` and never overwrites measurements. |
| Float parent creation | `get_or_create_float` selects by `wmo_number` first and creates parent before child `argo_profiles` insert. |
| Partial Stage B backfill failure | `run_stage_b_backfill` catches per-profile exceptions, records `failed` count and details, continues remaining profiles, and raises at the end so the operator can retry; subsequent runs skip already-updated vectors as `unchanged` with zero re-embedding. |

---

## 3. Dry-Run Fingerprints (Before vs After Audit)

Both dry-run executions (`python backend/scripts/bgc_promote_to_dev.py`) made
zero writes and confirmed identical development state:

| Metric | Before Audit (`dry_run_report.json`) | After Audit (`dry_run_report.json`) |
|---|---|---|
| Target DB / Collection | `argo_ocean_data_fresh` / `argo_float_profiles` | `argo_ocean_data_fresh` / `argo_float_profiles` |
| Schema V4 ready | `True` (39 columns) | `True` (39 columns) |
| `argo_profiles` / `argo_floats` | `1,384` / `22` | `1,384` / `22` (`max_profile_id=1384`, `max_float_id=22`) |
| SQL fingerprint | `pres=1353528, temp=1569851, psal=1550557, bgc=0` | `identity_hash=2951070014956, pres=1353528, temp=1569851, psal=1550557, bgc=0` |
| ChromaDB vectors | `1,384` (`1,384` needing backfill) | `1,384` (`1,384` needing backfill) |
| Corpus planned / collisions | `632` profiles / `0` collisions | `632` profiles / `0` collisions |
| Dry-run checks | `13 / 13 passed` | **`15 / 15 passed`** |

---

## 4. Separate Stage Execution Commands (DO NOT RUN Without Approval)

Run from `backend/` after creating backups:

* **Stage A — BGC Ingestion:**
  ```powershell
  python scripts/bgc_promote_to_dev.py --apply --stage ingest --mysql-backup backups\argo_ocean_data_fresh_pre_bgc_promotion.sql --chroma-backup backups\chroma_db_storage_fresh_pre_bgc_promotion
  ```
* **Stage B — Legacy Vector Backfill:**
  ```powershell
  python scripts/bgc_promote_to_dev.py --apply --stage backfill --mysql-backup backups\argo_ocean_data_fresh_pre_bgc_promotion.sql --chroma-backup backups\chroma_db_storage_fresh_pre_bgc_promotion
  ```

---

## 5. Rollback Summary

* **Preferred (exact ownership-verified rollback via `--rollback-stage-a`):**
  Reads `stage_a_manifest.json`, verifies every recorded profile ID still matches
  its manifest identity, verifies no unexpected children on promoted floats, verifies
  ChromaDB vector `profile_id_sql` metadata, and **refuses** on any mismatch.
  If all checks pass, deletes only the recorded vector IDs, profile rows (transactionally),
  and promoted float rows (only if unreferenced), restoring `1,384` profiles / `22` floats
  / `1,384` vectors without touching any other records.

  Run from `backend/`:
  ```powershell
  # Dry-run (verify ownership, no deletes):
  python scripts/bgc_promote_to_dev.py --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles

  # Applied (deletes only manifest-owned records):
  python scripts/bgc_promote_to_dev.py --apply --rollback-stage-a --manifest scratch\bgc_promotion\stage_a_manifest.json --expected-db argo_ocean_data_fresh --expected-collection argo_float_profiles
  ```

* **Emergency full restore (if Stage B or broader state must be reverted):**
  Run from `backend/`:
  ```powershell
  mysql -u <DB_USER> -p argo_ocean_data_fresh < backups\argo_ocean_data_fresh_pre_bgc_promotion.sql
  Remove-Item -Recurse -Force chroma_db_storage_fresh
  Copy-Item -Recurse -Path backups\chroma_db_storage_fresh_pre_bgc_promotion -Destination chroma_db_storage_fresh
  ```

## 6. Files Changed & Tests Executed

* **Modified:**
  * `backend/scripts/bgc_promote_to_dev.py`
  * `backend/test_bgc_promotion_and_correction.py`
  * `docs/bgc/BGC_DEVELOPMENT_PROMOTION_PLAN.md`
  * `backend/scratch/bgc_promotion/dry_run_report.json` (regenerated by dry-run)
* **Created:**
  * `docs/bgc/BGC_PROMOTION_SAFETY_AUDIT.md` (this report)
* **Unmodified:** `backend/data_processor.py`, `backend/database_manager.py`,
  `backend/bgc_matching.py`, migrations, NetCDF corpus, `argo_ocean_data_fresh`,
  and `backend/chroma_db_storage_fresh`.
* **Tests executed:**
  * `python backend/scripts/bgc_promote_to_dev.py` — **15 / 15 checks passed** (dry-run).
  * `python -m unittest test_bgc_promotion_and_correction -v` (from `backend/`) — **14 tests, OK** (100.731 s): covers exception-group corrections (3), dry-run non-mutation, backup verification failures (5 cases + 1 valid), collision/FK handling, existing-row preservation, Stage A SQL/vector crash recovery, Stage B backfill idempotency + partial failure retry, **exact rollback of manifest-owned records** (interleaved ID safety + pre-existing row preserved), **rollback refusal on unexpected records and ownership mismatch**, and **rollback preservation of concurrently allocated interleaved IDs**.

---

## 7. Remaining Limitations

1. Stage B `sync_profile_vector` re-embeds all `1,384` legacy documents
   because both metadata and document text differ from the legacy format.
2. `13` synthetic profiles remain flagged `pres_scale_suspect` in
   `bgc_source_metadata.grid_check` with raw pressures preserved.
3. Full Stage A + Stage B execution takes ~45–70 minutes locally and awaits
   explicit user approval.
