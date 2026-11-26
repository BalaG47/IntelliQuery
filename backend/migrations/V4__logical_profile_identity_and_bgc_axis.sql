-- ============================================================
-- Migration: V4 — Logical profile identity and the BGC pressure axis
-- Database:   argo_ocean_data_fresh
-- Applied to: argo_profiles
-- Author:     IntelliQuery unified core + BGC ingestion
-- Notes:
--   - One logical profile is (float_id, cycle_number, direction,
--     profile_time). The N_PROF records inside a core file are processing
--     variants of one physical profile, and the SD/SR product carries one
--     record per profile, so neither profile_index_in_file nor the product can
--     identify a profile (docs/bgc/BGC_PRODUCT_MATCHING.md).
--   - direction is NOT NULL DEFAULT '-': a unique key containing a NULL column
--     does not constrain anything in MySQL, and '-' is the documented "unknown"
--     value. Existing rows are filled by the DEFAULT, so no measurement or
--     provenance value is changed. backend/scripts/backfill_direction.py then
--     recovers 'A'/'D' from the stored source_file where that file is still
--     available.
--   - Audit of the development database before writing this file: 1,384 rows,
--     22 floats, 0 duplicate (float_id, cycle_number, profile_time) groups, so
--     the new unique key can be created without deleting or rewriting a row.
--     profile_time is already stored for every row; it does not need to be
--     recovered from the source files.
--   - bgc_pressure keeps the BGC product's own pressure axis. A BGC
--     measurement array is only interpretable with the pressure it arrived
--     with, so it is never interpolated onto the core grid.
--   - bgc_source_metadata keeps the BGC file, its N_PROF index, product type,
--     match rule and pressure-grid validation status
--     (ok / pres_scale_suspect / missing_pressure / grid_mismatch).
--   - Order matters: the wider key is added before the old one is dropped, so
--     the foreign key on float_id always has an index to use.
--   - Re-running this file fails on the duplicate column name; it is written to
--     be applied once.
-- ============================================================

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
