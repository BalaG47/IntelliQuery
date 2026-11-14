-- ============================================================
-- Migration: V2 — Add QC, Adjusted, Provenance, and BGC columns
-- Database:   argo_ocean_data_fresh
-- Applied to: argo_profiles table
-- Author:     IntelliQuery Phase 2A
-- Notes:
--   - Raw pressure/temperature/salinity columns are NOT modified.
--   - data_mode is nullable (NULL = not yet backfilled).
--   - profile_index_in_file is zero-based (matches Python xarray N_PROF index).
--   - (source_file_sha256, profile_index_in_file) is a non-unique lookup index.
--   - Domain uniqueness is preserved by existing UNIQUE KEY (float_id, cycle_number).
-- ============================================================

ALTER TABLE argo_profiles
  -- Source provenance
  ADD COLUMN source_file           VARCHAR(255)  DEFAULT NULL AFTER cycle_number,
  ADD COLUMN source_file_sha256    CHAR(64)      DEFAULT NULL AFTER source_file,
  ADD COLUMN profile_index_in_file INT           NOT NULL DEFAULT 0 AFTER source_file_sha256,
  ADD COLUMN source_metadata       JSON          DEFAULT NULL AFTER profile_index_in_file,

  -- Profile-level metadata
  ADD COLUMN data_mode             CHAR(1)       DEFAULT NULL AFTER source_metadata,
  ADD COLUMN profile_pres_qc      CHAR(1)       DEFAULT NULL AFTER data_mode,
  ADD COLUMN profile_temp_qc      CHAR(1)       DEFAULT NULL AFTER profile_pres_qc,
  ADD COLUMN profile_psal_qc      CHAR(1)       DEFAULT NULL AFTER profile_temp_qc,

  -- Pressure measurement-level QC and adjusted values
  ADD COLUMN pressure_qc           JSON          DEFAULT NULL AFTER pressure,
  ADD COLUMN pressure_adjusted     JSON          DEFAULT NULL AFTER pressure_qc,
  ADD COLUMN pressure_adjusted_error JSON        DEFAULT NULL AFTER pressure_adjusted,
  ADD COLUMN pressure_adjusted_qc  JSON          DEFAULT NULL AFTER pressure_adjusted_error,

  -- Temperature measurement-level QC and adjusted values
  ADD COLUMN temperature_qc        JSON          DEFAULT NULL AFTER temperature,
  ADD COLUMN temperature_adjusted  JSON          DEFAULT NULL AFTER temperature_qc,
  ADD COLUMN temperature_adjusted_error JSON     DEFAULT NULL AFTER temperature_adjusted,
  ADD COLUMN temperature_adjusted_qc JSON        DEFAULT NULL AFTER temperature_adjusted_error,

  -- Salinity measurement-level QC and adjusted values
  ADD COLUMN salinity_qc           JSON          DEFAULT NULL AFTER salinity,
  ADD COLUMN salinity_adjusted     JSON          DEFAULT NULL AFTER salinity_qc,
  ADD COLUMN salinity_adjusted_error JSON        DEFAULT NULL AFTER salinity_adjusted,
  ADD COLUMN salinity_adjusted_qc  JSON          DEFAULT NULL AFTER salinity_adjusted_error,

  -- Calibration metadata
  ADD COLUMN scientific_calib      JSON          DEFAULT NULL AFTER bgc_params,

  -- BGC parameter data mode and QC
  ADD COLUMN parameter_data_mode   JSON          DEFAULT NULL AFTER scientific_calib,
  ADD COLUMN bgc_qc                JSON          DEFAULT NULL AFTER parameter_data_mode,
  ADD COLUMN bgc_adjusted          JSON          DEFAULT NULL AFTER bgc_qc,
  ADD COLUMN bgc_adjusted_error    JSON          DEFAULT NULL AFTER bgc_adjusted,
  ADD COLUMN bgc_adjusted_qc       JSON          DEFAULT NULL AFTER bgc_adjusted_error,

  -- Provenance lookup index (non-unique: multiple profiles may share a file)
  ADD INDEX idx_profiles_source_hash (source_file_sha256, profile_index_in_file);
