-- ============================================================
-- Rollback: U2 — Remove QC, Adjusted, Provenance, and BGC columns
-- Reverses:  V2__add_qc_adjusted_provenance_and_bgc_columns.sql
-- Database:  argo_ocean_data_fresh
-- WARNING:   This rollback is only safe BEFORE backfill has been applied.
--            Running this after backfill (--apply) will permanently discard
--            all QC, adjusted, and provenance data.
-- ============================================================

ALTER TABLE argo_profiles
  DROP INDEX  idx_profiles_source_hash,
  DROP COLUMN bgc_adjusted_qc,
  DROP COLUMN bgc_adjusted_error,
  DROP COLUMN bgc_adjusted,
  DROP COLUMN bgc_qc,
  DROP COLUMN parameter_data_mode,
  DROP COLUMN scientific_calib,
  DROP COLUMN salinity_adjusted_qc,
  DROP COLUMN salinity_adjusted_error,
  DROP COLUMN salinity_adjusted,
  DROP COLUMN salinity_qc,
  DROP COLUMN temperature_adjusted_qc,
  DROP COLUMN temperature_adjusted_error,
  DROP COLUMN temperature_adjusted,
  DROP COLUMN temperature_qc,
  DROP COLUMN pressure_adjusted_qc,
  DROP COLUMN pressure_adjusted_error,
  DROP COLUMN pressure_adjusted,
  DROP COLUMN pressure_qc,
  DROP COLUMN profile_psal_qc,
  DROP COLUMN profile_temp_qc,
  DROP COLUMN profile_pres_qc,
  DROP COLUMN data_mode,
  DROP COLUMN source_metadata,
  DROP COLUMN profile_index_in_file,
  DROP COLUMN source_file_sha256,
  DROP COLUMN source_file;
