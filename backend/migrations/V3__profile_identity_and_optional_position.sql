-- ============================================================
-- Migration: V3 — Profile identity and optional position
-- Database:   argo_ocean_data_fresh
-- Applied to: argo_profiles
-- Author:     IntelliQuery BGC ingestion fixes
-- Notes:
--   - One Argo file may hold several N_PROF entries that all share one
--     CYCLE_NUMBER (ascending/descending samplings and parameter groups of a
--     single cycle). The old UNIQUE KEY (float_id, cycle_number) therefore
--     discarded every sub-profile after the first; profile identity is
--     (float_id, cycle_number, profile_index_in_file).
--   - profile_index_in_file is zero-based (Python xarray N_PROF index) and was
--     added by V2. Existing rows keep their value (0), so adding the wider key
--     cannot conflict as long as (float_id, cycle_number) was already unique.
--   - latitude/longitude become NULLable: a profile without a GPS fix keeps its
--     measurements instead of aborting the whole file. NULL means "unknown".
--     No coordinate is fabricated, and no existing row is modified.
--   - create_mysql_tables() creates the table with the old two-column unique
--     key, which MySQL names after its first column ('float_id'). Confirm the
--     name with `SHOW INDEX FROM argo_profiles;` and adjust the DROP INDEX
--     below if a database named it differently.
--   - Irreversible in practice for the constraint change (the rollback would
--     fail with duplicate key errors once sub-profiles exist), so no U3 file is
--     provided.
-- ============================================================

-- 1. A missing position fix is unknown, not an error.
ALTER TABLE argo_profiles
  MODIFY COLUMN latitude  FLOAT DEFAULT NULL,
  MODIFY COLUMN longitude FLOAT DEFAULT NULL;

-- 2. Add the real identity key first, so the foreign key on float_id keeps a
--    usable index when the old key is dropped.
ALTER TABLE argo_profiles
  ADD UNIQUE KEY uq_argo_profiles_identity
    (float_id, cycle_number, profile_index_in_file);

-- 3. Drop the old two-column key, which would still reject sub-profiles of one
--    cycle even with the wider key present.
ALTER TABLE argo_profiles
  DROP INDEX `float_id`;
