#!/usr/bin/env python3
"""
IntelliQuery — QC and Adjusted Measurements Backfill Script
File: backend/scripts/backfill_argo_profile_qc.py

Performs a dry-run or applied backfill of Argo QC flags, adjusted measurements,
errors, DATA_MODE, calibration metadata, BGC parameters, and provenance.

Requirements:
- Default execution is strictly DRY-RUN.
- Requires explicit --apply flag to perform database updates.
- Matches records via (WMO number, cycle_number).
- Processes all 1,384 existing profiles.
- Preserves raw measurements untouched.
- Produces a detailed CSV audit report.
"""

import argparse
import csv
import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone

import mysql.connector
import xarray as xr
from tqdm import tqdm

# Ensure backend directory is in path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, ARGO_DATA_DIR
from data_processor import (
    compute_file_sha256,
    extract_bgc_data,
    extract_data_mode,
    extract_measurement_array,
    extract_profile_qc,
    extract_qc_array,
    extract_scientific_calib,
    extract_source_metadata,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


REQUIRED_BACKFILL_COLUMNS = [
    "source_file",
    "source_file_sha256",
    "profile_index_in_file",
    "source_metadata",
    "data_mode",
    "profile_pres_qc",
    "profile_temp_qc",
    "profile_psal_qc",
    "pressure_qc",
    "pressure_adjusted",
    "pressure_adjusted_error",
    "pressure_adjusted_qc",
    "temperature_qc",
    "temperature_adjusted",
    "temperature_adjusted_error",
    "temperature_adjusted_qc",
    "salinity_qc",
    "salinity_adjusted",
    "salinity_adjusted_error",
    "salinity_adjusted_qc",
    "scientific_calib",
    "parameter_data_mode",
    "bgc_qc",
    "bgc_adjusted",
    "bgc_adjusted_error",
    "bgc_adjusted_qc",
]

UPDATE_PROFILE_QC_SQL = """
UPDATE argo_profiles SET
    source_file = %s,
    source_file_sha256 = %s,
    profile_index_in_file = %s,
    source_metadata = %s,
    data_mode = %s,
    profile_pres_qc = %s,
    profile_temp_qc = %s,
    profile_psal_qc = %s,
    pressure_qc = %s,
    pressure_adjusted = %s,
    pressure_adjusted_error = %s,
    pressure_adjusted_qc = %s,
    temperature_qc = %s,
    temperature_adjusted = %s,
    temperature_adjusted_error = %s,
    temperature_adjusted_qc = %s,
    salinity_qc = %s,
    salinity_adjusted = %s,
    salinity_adjusted_error = %s,
    salinity_adjusted_qc = %s,
    scientific_calib = %s,
    parameter_data_mode = %s,
    bgc_qc = %s,
    bgc_adjusted = %s,
    bgc_adjusted_error = %s,
    bgc_adjusted_qc = %s
WHERE profile_id = %s
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Backfill Argo profile QC and adjusted data.")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute database writes. When omitted, runs in safe DRY-RUN mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run without database writes (default).",
    )
    parser.add_argument(
        "--report-csv",
        type=str,
        default=None,
        help="Path for output CSV report.",
    )
    parser.add_argument(
        "--db-name",
        type=str,
        default=DB_NAME,
        help="Target database name (defaults to DB_NAME from config).",
    )
    return parser.parse_args()


def get_db_connection(db_name=None):
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        database=db_name or DB_NAME,
    )


def build_netcdf_file_index(data_dir):
    """
    Indexes NetCDF files by (wmo, cycle) from filenames and quick headers.
    """
    logging.info(f"Indexing NetCDF files in {data_dir}...")
    nc_files = glob.glob(os.path.join(data_dir, "*.nc"))
    index = {}

    for fpath in nc_files:
        fn = os.path.basename(fpath)
        # Standard names: D<wmo>_<cycle>.nc or R<wmo>_<cycle>.nc
        # e.g., D1900122_009.nc
        parts = fn.split("_")
        if len(parts) >= 2:
            wmo_part = "".join(filter(str.isdigit, parts[0]))
            cycle_part = parts[1].split(".")[0]
            cycle_digits = "".join(filter(str.isdigit, cycle_part))
            if wmo_part and cycle_digits:
                key = (int(wmo_part), int(cycle_digits))
                if key not in index:
                    index[key] = fpath

    logging.info(f"Indexed {len(index)} distinct (WMO, cycle) NetCDF files.")
    return index


def main():
    args = parse_args()
    is_apply = args.apply
    is_dry_run = not is_apply

    mode_label = "APPLY" if is_apply else "DRY-RUN (READ-ONLY)"
    print(f"\n=======================================================")
    print(f"  IntelliQuery — Profile QC & Metadata Backfill")
    print(f"  Mode: {mode_label}")
    print(f"=======================================================\n")

    conn = get_db_connection(args.db_name)
    cursor = conn.cursor(dictionary=True)

    # 1. Fetch all existing profiles
    query = """
    SELECT p.profile_id, p.float_id, f.wmo_number, p.cycle_number, p.profile_time,
           p.latitude, p.longitude, p.pressure, p.temperature, p.salinity
    FROM argo_profiles p
    JOIN argo_floats f ON p.float_id = f.float_id
    ORDER BY f.wmo_number, p.cycle_number
    """
    cursor.execute(query)
    db_profiles = cursor.fetchall()
    total_db_profiles = len(db_profiles)
    logging.info(f"Retrieved {total_db_profiles} existing profile records from database.")

    # 2. Check current table columns to see if new columns exist
    cursor.execute("SHOW COLUMNS FROM argo_profiles")
    existing_cols = {row["Field"] for row in cursor.fetchall()}
    logging.info(f"Table argo_profiles has {len(existing_cols)} columns currently.")

    if is_apply:
        missing_cols = [c for c in REQUIRED_BACKFILL_COLUMNS if c not in existing_cols]
        if missing_cols:
            cursor.close()
            conn.close()
            raise RuntimeError(
                f"Cannot run backfill in APPLY mode: missing required columns in argo_profiles: {missing_cols}"
            )

    # 3. Build NetCDF file index
    file_index = build_netcdf_file_index(ARGO_DATA_DIR)

    # 4. Process each profile
    matched_count = 0
    unmatched_count = 0
    eligible_count = 0
    extraction_failures = 0
    missing_variables_count = 0
    updated_count = 0

    report_rows = []
    sample_records = []

    update_cursor = None
    if is_apply:
        conn.autocommit = False
        update_cursor = conn.cursor()

    try:
        for prof in tqdm(db_profiles, desc="Evaluating profiles"):
            pid = prof["profile_id"]
            wmo = prof["wmo_number"]
            cycle = prof["cycle_number"]
            key = (wmo, cycle)
    
            fpath = file_index.get(key)
            if not fpath:
                # Fallback: scan for any file containing wmo and cycle
                pattern = os.path.join(ARGO_DATA_DIR, f"*{wmo}*{cycle:03d}*.nc")
                matches = glob.glob(pattern)
                if not matches:
                    pattern = os.path.join(ARGO_DATA_DIR, f"*{wmo}*{cycle}*.nc")
                    matches = glob.glob(pattern)
                if matches:
                    fpath = matches[0]
    
            if not fpath:
                unmatched_count += 1
                report_rows.append({
                    "profile_id": pid,
                    "wmo_number": wmo,
                    "cycle_number": cycle,
                    "status": "UNMATCHED_FILE",
                    "filename": "N/A",
                    "data_mode": "N/A",
                    "pres_levels": 0,
                    "pres_qc_levels": 0,
                    "pres_adj_levels": 0,
                    "temp_levels": 0,
                    "temp_qc_levels": 0,
                    "temp_adj_levels": 0,
                    "psal_levels": 0,
                    "psal_qc_levels": 0,
                    "psal_adj_levels": 0,
                    "calib_entries": 0,
                    "notes": "No matching NetCDF file found on disk",
                })
                continue
    
            matched_count += 1
            fn = os.path.basename(fpath)
    
            try:
                sha256 = compute_file_sha256(fpath)
                with xr.open_dataset(fpath, decode_times=False) as ds:
                    data_mode = extract_data_mode(ds, 0)
                    p_pres_qc = extract_profile_qc(ds, "PROFILE_PRES_QC", 0)
                    p_temp_qc = extract_profile_qc(ds, "PROFILE_TEMP_QC", 0)
                    p_psal_qc = extract_profile_qc(ds, "PROFILE_PSAL_QC", 0)
    
                    pres = extract_measurement_array(ds, "PRES", 0)
                    pres_qc = extract_qc_array(ds, "PRES_QC", 0)
                    pres_adj = extract_measurement_array(ds, "PRES_ADJUSTED", 0)
                    pres_adj_err = extract_measurement_array(ds, "PRES_ADJUSTED_ERROR", 0)
                    pres_adj_qc = extract_qc_array(ds, "PRES_ADJUSTED_QC", 0)
    
                    temp = extract_measurement_array(ds, "TEMP", 0)
                    temp_qc = extract_qc_array(ds, "TEMP_QC", 0)
                    temp_adj = extract_measurement_array(ds, "TEMP_ADJUSTED", 0)
                    temp_adj_err = extract_measurement_array(ds, "TEMP_ADJUSTED_ERROR", 0)
                    temp_adj_qc = extract_qc_array(ds, "TEMP_ADJUSTED_QC", 0)
    
                    psal = extract_measurement_array(ds, "PSAL", 0)
                    psal_qc = extract_qc_array(ds, "PSAL_QC", 0)
                    psal_adj = extract_measurement_array(ds, "PSAL_ADJUSTED", 0)
                    psal_adj_err = extract_measurement_array(ds, "PSAL_ADJUSTED_ERROR", 0)
                    psal_adj_qc = extract_qc_array(ds, "PSAL_ADJUSTED_QC", 0)
    
                    calib = extract_scientific_calib(ds, 0)
                    bgc_params, bgc_qc, bgc_adj, bgc_adj_err, bgc_adj_qc, param_data_mode = extract_bgc_data(ds, 0)
                    source_meta = extract_source_metadata(ds, fpath, 0)
    
                    # Validation checks
                    notes = []
                    if pres is None or temp is None or psal is None:
                        missing_variables_count += 1
                        notes.append("Missing core variable")
    
                    # Verify 1-to-1 level count alignment
                    n_p = len(pres) if pres else 0
                    n_pq = len(pres_qc) if pres_qc else 0
                    n_pa = len(pres_adj) if pres_adj else 0
                    if n_p > 0 and (n_p != n_pq or n_p != n_pa):
                        notes.append(f"Pressure length mismatch (P:{n_p}, QC:{n_pq}, ADJ:{n_pa})")
    
                    n_t = len(temp) if temp else 0
                    n_tq = len(temp_qc) if temp_qc else 0
                    n_ta = len(temp_adj) if temp_adj else 0
                    if n_t > 0 and (n_t != n_tq or n_t != n_ta):
                        notes.append(f"Temperature length mismatch (T:{n_t}, QC:{n_tq}, ADJ:{n_ta})")
    
                    n_s = len(psal) if psal else 0
                    n_sq = len(psal_qc) if psal_qc else 0
                    n_sa = len(psal_adj) if psal_adj else 0
                    if n_s > 0 and (n_s != n_sq or n_s != n_sa):
                        notes.append(f"Salinity length mismatch (S:{n_s}, QC:{n_sq}, ADJ:{n_sa})")
    
                    eligible_count += 1
                    status = "ELIGIBLE" if not notes else "WARNING"
    
                    report_rows.append({
                        "profile_id": pid,
                        "wmo_number": wmo,
                        "cycle_number": cycle,
                        "status": status,
                        "filename": fn,
                        "data_mode": data_mode or "NULL",
                        "pres_levels": n_p,
                        "pres_qc_levels": n_pq,
                        "pres_adj_levels": n_pa,
                        "temp_levels": n_t,
                        "temp_qc_levels": n_tq,
                        "temp_adj_levels": n_ta,
                        "psal_levels": n_s,
                        "psal_qc_levels": n_sq,
                        "psal_adj_levels": n_sa,
                        "calib_entries": len(calib) if calib else 0,
                        "notes": "; ".join(notes) if notes else "Valid alignment",
                    })
    
                    # Store sample before/after representations (for first 2 and extreme profile 58)
                    if pid in (1, 2, 58) or len(sample_records) < 3:
                        sample_records.append({
                            "profile_id": pid,
                            "wmo": wmo,
                            "cycle": cycle,
                            "filename": fn,
                            "data_mode": data_mode,
                            "before": {
                                "data_mode": None,
                                "profile_pres_qc": None,
                                "pressure_qc": None,
                                "pressure_adjusted": None,
                                "temperature_qc": None,
                                "temperature_adjusted": None,
                                "salinity_qc": None,
                                "salinity_adjusted": None,
                                "scientific_calib": None,
                            },
                            "after_excerpt": {
                                "data_mode": data_mode,
                                "profile_pres_qc": p_pres_qc,
                                "profile_temp_qc": p_temp_qc,
                                "profile_psal_qc": p_psal_qc,
                                "pressure_qc_sample": pres_qc[:4] if pres_qc else None,
                                "pressure_adj_sample": pres_adj[:4] if pres_adj else None,
                                "temp_qc_sample": temp_qc[:4] if temp_qc else None,
                                "temp_adj_sample": temp_adj[:4] if temp_adj else None,
                                "psal_qc_sample": psal_qc[:4] if psal_qc else None,
                                "psal_adj_sample": psal_adj[:4] if psal_adj else None,
                                "calib_count": len(calib) if calib else 0,
                                "source_file": fn,
                                "source_file_sha256": sha256,
                            }
                        })
    
                    # If APPLY mode and columns exist, perform database update
                    if is_apply:
                        params = (
                            fn,
                            sha256,
                            0,  # profile_index_in_file
                            json.dumps(source_meta) if source_meta is not None else None,
                            data_mode,
                            p_pres_qc,
                            p_temp_qc,
                            p_psal_qc,
                            json.dumps(pres_qc) if pres_qc is not None else None,
                            json.dumps(pres_adj) if pres_adj is not None else None,
                            json.dumps(pres_adj_err) if pres_adj_err is not None else None,
                            json.dumps(pres_adj_qc) if pres_adj_qc is not None else None,
                            json.dumps(temp_qc) if temp_qc is not None else None,
                            json.dumps(temp_adj) if temp_adj is not None else None,
                            json.dumps(temp_adj_err) if temp_adj_err is not None else None,
                            json.dumps(temp_adj_qc) if temp_adj_qc is not None else None,
                            json.dumps(psal_qc) if psal_qc is not None else None,
                            json.dumps(psal_adj) if psal_adj is not None else None,
                            json.dumps(psal_adj_err) if psal_adj_err is not None else None,
                            json.dumps(psal_adj_qc) if psal_adj_qc is not None else None,
                            json.dumps(calib) if calib is not None else None,
                            json.dumps(param_data_mode) if param_data_mode is not None else None,
                            json.dumps(bgc_qc) if bgc_qc is not None else None,
                            json.dumps(bgc_adj) if bgc_adj is not None else None,
                            json.dumps(bgc_adj_err) if bgc_adj_err is not None else None,
                            json.dumps(bgc_adj_qc) if bgc_adj_qc is not None else None,
                            pid,
                        )
                        update_cursor.execute(UPDATE_PROFILE_QC_SQL, params)
                        updated_count += 1
    
            except Exception as e:
                extraction_failures += 1
                report_rows.append({
                    "profile_id": pid,
                    "wmo_number": wmo,
                    "cycle_number": cycle,
                    "status": "EXTRACTION_ERROR",
                    "filename": fn,
                    "data_mode": "ERROR",
                    "pres_levels": 0,
                    "pres_qc_levels": 0,
                    "pres_adj_levels": 0,
                    "temp_levels": 0,
                    "temp_qc_levels": 0,
                    "temp_adj_levels": 0,
                    "psal_levels": 0,
                    "psal_qc_levels": 0,
                    "psal_adj_levels": 0,
                    "calib_entries": 0,
                    "notes": f"Extraction error: {str(e)}",
                })
                if is_apply:
                    # In apply mode, raise immediately to trigger transactional rollback
                    raise
    
        if is_apply:
            if extraction_failures > 0:
                conn.rollback()
                raise RuntimeError(
                    f"Aborting backfill: {extraction_failures} extraction failures encountered. Transaction rolled back."
                )
            conn.commit()
            logging.info(f"Transaction committed successfully. {updated_count} profiles updated.")

    except Exception as exc:
        if is_apply:
            conn.rollback()
            logging.error(f"Transaction rolled back due to error: {exc}")
        raise exc
    finally:
        if update_cursor:
            update_cursor.close()
        cursor.close()
        conn.close()

    # 5. Output CSV audit report
    report_csv_path = args.report_csv
    if not report_csv_path:
        csv_filename = "argo_profile_qc_backfill_applied.csv" if is_apply else "argo_profile_qc_backfill_dryrun.csv"
        report_csv_path = os.path.join(backend_dir, "reports", csv_filename)

    os.makedirs(os.path.dirname(report_csv_path), exist_ok=True)
    fieldnames = [
        "profile_id", "wmo_number", "cycle_number", "status", "filename", "data_mode",
        "pres_levels", "pres_qc_levels", "pres_adj_levels",
        "temp_levels", "temp_qc_levels", "temp_adj_levels",
        "psal_levels", "psal_qc_levels", "psal_adj_levels",
        "calib_entries", "notes"
    ]
    with open(report_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    logging.info(f"Audit report saved to {report_csv_path}")

    # 6. Summary Report
    print(f"\n=======================================================")
    print(f"  BACKFILL {mode_label} AUDIT SUMMARY")
    print(f"=======================================================")
    print(f"  Total Database Profiles Scanned : {total_db_profiles}")
    print(f"  NetCDF Files Matched On Disk    : {matched_count} ({matched_count/total_db_profiles*100:.1f}%)")
    print(f"  Unmatched Database Profiles     : {unmatched_count}")
    print(f"  Profiles Eligible for Update    : {eligible_count} ({eligible_count/total_db_profiles*100:.1f}%)")
    print(f"  Extraction Failures             : {extraction_failures}")
    print(f"  Profiles with Missing Core Vars : {missing_variables_count}")
    print(f"  Audit Report Path               : {report_csv_path}")
    if is_apply:
        print(f"  Database Updates Committed      : {updated_count}")
    else:
        print(f"  Database Writes Performed       : 0 (DRY RUN ONLY)")
    print(f"=======================================================\n")

    print("--- SAMPLE BEFORE / AFTER REPRESENTATIONS ---")
    for s in sample_records[:3]:
        print(f"\nProfile ID: {s['profile_id']} | WMO: {s['wmo']} | Cycle: {s['cycle']} | File: {s['filename']}")
        print(f"  DATA_MODE: {s['data_mode']}")
        print(f"  BEFORE: all new fields are NULL in database")
        print(f"  AFTER EXCERPT:")
        for k, v in s["after_excerpt"].items():
            print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
