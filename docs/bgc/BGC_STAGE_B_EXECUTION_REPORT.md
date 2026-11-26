# IntelliQuery — BGC Promotion Stage B Execution Report

**Execution Date & Time:** 2026-11-26 (Started ~15:23 IST; Completed `2026-11-26T09:56:18.350522+00:00` / 15:26 IST)  
**Stage Executed:** **Stage B — Legacy ChromaDB Metadata Backfill Only** (`--apply --stage backfill`)  
**Target MySQL Database:** `argo_ocean_data_fresh` (`localhost:3306`)  
**Target ChromaDB Store & Collection:** `backend/chroma_db_storage_fresh` / `argo_float_profiles`

---

## 1. Pre-Execution Baseline & Dry-Run Inspection

Running `python scripts/bgc_promote_to_dev.py` in dry-run mode against the post-Stage-A state confirmed:

* **SQL profiles / floats:** `2,016` profiles (`632` BGC, `1,384` core), `30` floats (`1,384` legacy + `632` Stage A).
* **SQL fingerprint:** `rows_total=2016`, `floats_total=30`, `max_profile_id=2016`, `max_float_id=30`, `identity_hash=4343819274346`, `pres=4258654`, `temp=4423159`, `psal=4624256`, `bgc=11516578`.
* **ChromaDB vectors:** `2,016` (`0` missing, `0` extra).
* **Vector metadata status before Stage B:** `632` Stage A vectors already up to date; **`1,384` legacy vectors requiring backfill**.
* **Dry-run checks:** `14 / 15 passed` — the only non-passing check was `zero_identity_collisions_with_dev` (`632` collisions, `0` eligible), which is expected because Stage A had already ingested the 632 corpus profiles.

---

## 2. Fresh Post-Stage-A Backups & Verification

Separate post-Stage-A / pre-Stage-B backups were created in `backend/backups/` while preserving the earlier pre-Stage-A backups:

| Store | Backup Path | Verification Result |
|---|---|---|
| MySQL (`argo_ocean_data_fresh`) | `backend/backups/argo_ocean_data_fresh_post_stage_a_pre_stage_b.sql` | Exists, regular file, **`110,386,201` bytes** (non-empty) |
| ChromaDB (`chroma_db_storage_fresh`) | `backend/backups/chroma_db_storage_fresh_post_stage_a_pre_stage_b` | Exists, `chroma.sqlite3` **`88,322,048` bytes**, collection `argo_float_profiles` present with **`2,016` vectors** |

---

## 3. Stage B Execution Command & Results

Executed from `backend/`:

```powershell
python scripts/bgc_promote_to_dev.py --apply --stage backfill --mysql-backup "backups\argo_ocean_data_fresh_post_stage_a_pre_stage_b.sql" --chroma-backup "backups\chroma_db_storage_fresh_post_stage_a_pre_stage_b"
```

### Expected vs. Actual Measured Results

| Metric | Expected | Actual Measured | Status |
|---|---:|---:|---|
| Profiles scanned | 2,016 | 2,016 | PASS |
| Legacy vectors updated/re-embedded | 1,384 | 1,384 | PASS |
| Stage A vectors unchanged | 632 | 632 | PASS |
| Vectors recreated (missing before) | 0 | 0 (`missing_before=0`, `missing_after=0`) | PASS |
| Failed vector synchronizations | 0 | 0 (`failures=[]`) | PASS |
| Final SQL profiles | 2,016 | 2,016 | PASS |
| Final SQL floats | 30 | 30 | PASS |
| Final ChromaDB vectors | 2,016 | 2,016 (`2,016` unique IDs) | PASS |
| Metadata & identity parity errors | 0 | 0 | PASS |

---

## 4. Independent Post-Execution Verification

Independent verification across all `2,016` SQL rows and all `2,016` ChromaDB vectors confirmed:

* **Counts & uniqueness:** `2,016` SQL profiles, `30` SQL floats, `0` duplicate logical identities, `2,016` ChromaDB vectors, `0` missing vectors, `0` extra vectors.
* **Metadata & identity parity:** **`0` errors** across all `1,384` legacy vectors and `632` Stage A vectors:
  * `profile_id_sql`, `float_id_sql` (WMO number), `cycle_number`, and `direction` match MySQL for every profile.
  * All `1,384` legacy vectors (`profile_1`–`profile_1384`) now have `has_bgc=False`, `product_type='core'`, `bgc_parameters=''`, and `bgc_parameter_count=0`.
  * All `632` Stage A vectors (`profile_1385`–`profile_2016`) remain intact with `has_bgc=True`, `product_type='core_bgc'`, and `bgc_parameters` / `bgc_parameter_count` matching SQL `bgc_params`.
* **Measurement preservation:** Full 2,016-row SQL fingerprint after Stage B (`rows_total=2016`, `max_profile_id=2016`, `identity_hash=4343819274346`, `pres=4258654`, `temp=4423159`, `psal=4624256`, `bgc=11516578`) is **identical** to the pre-Stage-B fingerprint.
* **Pressure anomaly handling:** All **`13` `pres_scale_suspect` profiles** remain flagged in `bgc_source_metadata.grid_check` with raw measurements unmodified.
* **Original legacy database:** `argo_ocean_data` remains untouched (`45` profiles).

---

## 5. Idempotency & Regression Test Verification

* **Post-Stage-B dry-run (`python scripts/bgc_promote_to_dev.py`):**
  * Reported `Dev baseline: 2016 profiles, 30 floats, 2016 vectors`, `vectors_up_to_date_metadata: 2016`, and **`Dev vectors needing backfill: 0`**, with identical SQL fingerprint (`identity_hash=4343819274346`).
  * *Limitation of dry-run mode:* `inspect_development_state()` checks metadata keys (`direction`, `has_bgc`, `product_type`, `bgc_parameters`, `bgc_parameter_count`) rather than re-computing full document text equality; full `sync_profile_vector()` document+metadata idempotency (returning `unchanged` with zero re-embedding) is directly covered by `test_stage_b_backfill_idempotency_and_partial_failure_retry`.
* **Regression test suite (`python -m unittest test_bgc_promotion_and_correction -v` from `backend/`):**
  * **14 / 14 tests passed (`OK` in 132.582 s)**, covering exception-group corrections, dry-run non-mutation, backup verification, collision/FK handling, existing-row preservation, Stage A crash recovery, Stage B idempotency & partial-failure retry, and exact manifest-owned rollback.

---

## 6. Warnings & Limitations

* **Cosmetic PowerShell exit code `1`:** `huggingface_hub` emitted an informational unauthenticated-request warning on stderr when loading the locally cached embedding model, causing PowerShell `2>&1` wrappers to report `NativeCommandError` (`exit code 1`) despite clean Python completion (`failed: 0`). Similarly, `unittest` logged `WARNING:root:` messages to stderr during simulated crash-recovery tests while passing all 14 tests.
* **No further steps executed:** Stopped after Stage B as instructed; no Insights calculations, query architecture changes, embedding model changes, or additional optimizations were performed.
