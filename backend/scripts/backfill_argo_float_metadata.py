#!/usr/bin/env python3
"""
ARGO Float Metadata Backfill Script for IntelliQuery.

Inspects source NetCDF files for the 22 existing floats in `argo_floats`
and backfills `project_name` and `platform_type`.

Usage:
    python backfill_argo_float_metadata.py [--dry-run]
    python backfill_argo_float_metadata.py --apply
"""

import argparse
import csv
import glob
import os
import re
import sys
from pathlib import Path

# Add backend directory to sys.path so imports work
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import numpy as np
import xarray as xr
from sqlalchemy import create_engine, text

from config import (
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME,
    ARGO_DATA_DIR,
)
from data_processor import extract_netcdf_metadata_string


def get_db_engine():
    """Create SQLAlchemy engine using backend config."""
    db_url = f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(db_url)


def find_files_for_wmo(data_dir, wmo_number):
    """
    Find all NetCDF files belonging to a specific WMO.
    Matches standard ARGO naming: D<wmo>_<cycle>.nc or R<wmo>_<cycle>.nc.
    """
    wmo_pattern = re.compile(rf"^[DR]{wmo_number}_\d+\.nc$", re.IGNORECASE)
    all_files = os.listdir(data_dir)
    matched = []
    for f in all_files:
        if wmo_pattern.match(f):
            matched.append(os.path.join(data_dir, f))
    return sorted(matched)


def inspect_source_metadata_for_wmo(files):
    """
    Inspects all source files for a WMO and extracts distinct metadata values.
    Returns:
        (project_name, platform_type, status, details)
    """
    if not files:
        return None, None, "MISSING_SOURCE_METADATA", "No NetCDF files found for WMO"

    distinct_projects = set()
    distinct_platforms = set()

    for fpath in files:
        try:
            with xr.open_dataset(fpath, decode_times=False) as ds:
                proj = extract_netcdf_metadata_string(ds, "PROJECT_NAME", default="N/A")
                plat = extract_netcdf_metadata_string(ds, "PLATFORM_TYPE", default="N/A")
                distinct_projects.add(proj)
                distinct_platforms.add(plat)
        except Exception as e:
            return None, None, "SOURCE_READ_ERROR", f"Error reading {os.path.basename(fpath)}: {str(e)}"

    # Check for consistency
    valid_projects = [p for p in distinct_projects if p != "N/A"]
    valid_platforms = [p for p in distinct_platforms if p != "N/A"]

    if len(valid_projects) > 1 or len(valid_platforms) > 1:
        return None, None, "INCONSISTENT_SOURCE_METADATA", f"Multiple distinct values found: projects={valid_projects}, platforms={valid_platforms}"

    if not valid_projects and not valid_platforms:
        return None, None, "MISSING_SOURCE_METADATA", "All source files returned N/A for metadata"

    final_proj = valid_projects[0] if valid_projects else "N/A"
    final_plat = valid_platforms[0] if valid_platforms else "N/A"

    return final_proj, final_plat, "CONSISTENT", f"Verified across {len(files)} files"


def run_backfill(dry_run=True):
    engine = get_db_engine()
    data_dir = os.path.join(BACKEND_DIR, ARGO_DATA_DIR)

    if not os.path.isdir(data_dir):
        print(f"Error: ARGO data directory not found at {data_dir}", file=sys.stderr)
        sys.exit(1)

    print("=" * 80)
    print(f"ARGO Float Metadata Backfill — {'DRY RUN' if dry_run else 'APPLY MODE'}")
    print(f"Data directory: {data_dir}")
    print("=" * 80)

    # 1. Read existing floats from database
    with engine.connect() as conn:
        db_floats = conn.execute(
            text("SELECT float_id, wmo_number, project_name, platform_type FROM argo_floats ORDER BY float_id")
        ).fetchall()

    total_inspected = len(db_floats)
    eligible_count = 0
    already_correct_count = 0
    missing_source_count = 0
    inconsistent_count = 0
    manual_review_count = 0

    report_rows = []
    updates_to_apply = []

    for row in db_floats:
        float_id, wmo, old_proj, old_plat = row[0], row[1], row[2], row[3]
        old_proj_clean = "N/A" if old_proj is None or str(old_proj).strip() in ("", "N/A") else str(old_proj).strip()
        old_plat_clean = "N/A" if old_plat is None or str(old_plat).strip() in ("", "N/A") else str(old_plat).strip()

        # Find and inspect source files
        files = find_files_for_wmo(data_dir, wmo)
        new_proj, new_plat, consistency_status, details = inspect_source_metadata_for_wmo(files)

        if consistency_status == "INCONSISTENT_SOURCE_METADATA":
            status = "MANUAL_REVIEW_INCONSISTENT"
            inconsistent_count += 1
            manual_review_count += 1
            target_proj, target_plat = old_proj_clean, old_plat_clean
        elif consistency_status in ("MISSING_SOURCE_METADATA", "SOURCE_READ_ERROR"):
            status = "MISSING_SOURCE_METADATA"
            missing_source_count += 1
            target_proj, target_plat = old_proj_clean, old_plat_clean
        else:
            # Consistent metadata found
            target_proj = new_proj
            target_plat = new_plat
            # Check if update is needed
            if old_proj_clean == new_proj and old_plat_clean == new_plat:
                status = "ALREADY_CORRECT"
                already_correct_count += 1
            else:
                status = "ELIGIBLE_FOR_UPDATE"
                eligible_count += 1
                updates_to_apply.append({
                    "float_id": float_id,
                    "wmo_number": wmo,
                    "project_name": new_proj,
                    "platform_type": new_plat,
                })

        report_rows.append({
            "WMO": wmo,
            "Old Project": old_proj_clean,
            "New Project": target_proj,
            "Old Platform": old_plat_clean,
            "New Platform": target_plat,
            "Status": status,
        })

    # Print Table
    print(f"\n{'WMO':<10} | {'Old Project':<15} | {'New Project':<18} | {'Old Platform':<14} | {'New Platform':<14} | {'Status':<24}")
    print("-" * 105)
    for r in report_rows:
        print(f"{r['WMO']:<10} | {r['Old Project']:<15} | {r['New Project']:<18} | {r['Old Platform']:<14} | {r['New Platform']:<14} | {r['Status']:<24}")

    # Summary
    print("\n" + "=" * 50)
    print("BACKFILL SUMMARY STATISTICS")
    print("=" * 50)
    print(f"Total float records inspected:     {total_inspected}")
    print(f"Records eligible for correction:   {eligible_count}")
    print(f"Records already correct:           {already_correct_count}")
    print(f"Records with missing metadata:     {missing_source_count}")
    print(f"Records with inconsistent metadata:{inconsistent_count}")
    print(f"Records requiring manual review:   {manual_review_count}")
    print("=" * 50)

    # Save CSV Report
    reports_dir = os.path.join(BACKEND_DIR, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_csv_path = os.path.join(reports_dir, "argo_float_metadata_backfill_report.csv")
    with open(report_csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["WMO", "Old Project", "New Project", "Old Platform", "New Platform", "Status"])
        writer.writeheader()
        writer.writerows(report_rows)
    print(f"\nDry-run report saved to: {report_csv_path}")

    # Execute updates only if apply mode is explicitly passed
    if not dry_run:
        print("\nApplying database updates in a single transaction...")
        try:
            with engine.begin() as conn:
                for u in updates_to_apply:
                    conn.execute(
                        text("""
                            UPDATE argo_floats
                            SET project_name = :project_name,
                                platform_type = :platform_type
                            WHERE wmo_number = :wmo_number
                              AND float_id = :float_id
                        """),
                        u
                    )
                    print(f"  [UPDATED] WMO {u['wmo_number']}: project='{u['project_name']}', platform='{u['platform_type']}'")
            print("\nSuccessfully applied all updates! Transaction committed.")
        except Exception as e:
            print(f"\nError applying updates (transaction rolled back): {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("\n[DRY RUN COMPLETE] No database records have been modified.")
        print("To apply these changes, pass --apply after receiving approval.")


def main():
    parser = argparse.ArgumentParser(description="Backfill ARGO float metadata in argo_floats.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run in dry-run mode (default, no DB changes)")
    parser.add_argument("--apply", action="store_true", help="Apply updates to database")

    args = parser.parse_args()
    dry_run_mode = not args.apply

    run_backfill(dry_run=dry_run_mode)


if __name__ == "__main__":
    main()
