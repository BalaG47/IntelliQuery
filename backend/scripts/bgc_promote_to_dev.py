"""Dry-run validator and two-stage promotion runner for BGC-Argo -> development.

By default this script runs in **dry-run mode** (read-only against the target
development MySQL database and ChromaDB collection). Any database or vector
store mutation requires ``--apply`` together with verified MySQL and ChromaDB
backup artifacts (``--mysql-backup`` and ``--chroma-backup``).

Stages:
  * **Dry-run (default):** Verifies V4 schema, baseline fingerprints, 1,264-file
    corpus plans, 0 identity collisions, 8 new float FKs, 13 pressure-grid
    anomalies, and legacy vector backfill count.
  * **Stage A (``--apply --stage ingest``):** Ingests the 632 complete logical
    profiles into MySQL and creates their 632 vectors via
    ``ArgoDataProcessor.process_paths``, preserving all existing rows and
    verifying 2,016 SQL rows and 2,016 vectors.
  * **Stage B (``--apply --stage backfill``):** Runs resumable, idempotent
    ``DatabaseManager.sync_profile_vector`` across stored profiles to upgrade
    legacy vectors to the current document/metadata format, reporting
    ``updated``, ``unchanged``, ``recreated``, ``missing``, and ``failed``
    counts separately.

Run from ``backend/``:

    python scripts/bgc_promote_to_dev.py
    python scripts/bgc_promote_to_dev.py --apply --stage ingest --mysql-backup backups/pre_bgc.sql --chroma-backup backups/chroma_pre_bgc
    python scripts/bgc_promote_to_dev.py --apply --stage backfill --mysql-backup backups/pre_bgc.sql --chroma-backup backups/chroma_pre_bgc
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus

import chromadb
import netCDF4
import numpy as np
from sqlalchemy import create_engine, text

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bgc_matching import (  # noqa: E402
    GRID_PRES_SCALE_SUSPECT,
    UNKNOWN_DIRECTION,
    classify_pressure_grid,
    group_by_cycle,
    index_file,
    plan_group,
)
from config import (  # noqa: E402
    CHROMA_COLLECTION_NAME,
    CHROMA_PERSIST_DIR,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    require_database_config,
)
from data_processor import (  # noqa: E402
    ArgoDataProcessor,
    is_core_auxiliary_parameter,
)
from database_manager import DatabaseManager, _redact  # noqa: E402

DEFAULT_DATA_ROOT = os.environ.get(
    "BGC_DATA_DIR", os.path.join(BACKEND_DIR, "bgc data", "bgc raw data")
)
if not os.path.isabs(DEFAULT_DATA_ROOT):
    DEFAULT_DATA_ROOT = os.path.join(BACKEND_DIR, DEFAULT_DATA_ROOT)

DEFAULT_REPORT_PATH = os.path.join(
    BACKEND_DIR, "scratch", "bgc_promotion", "dry_run_report.json"
)
DEFAULT_STAGE_A_MANIFEST = os.path.join(
    BACKEND_DIR, "scratch", "bgc_promotion", "stage_a_manifest.json"
)

EXPECTED_TARGET_DB = "argo_ocean_data_fresh"
EXPECTED_TARGET_COLLECTION = "argo_float_profiles"

EXPECTED_CORPUS_FILES = 1264
EXPECTED_GROUPS = 626
EXPECTED_PROFILES = 632
EXPECTED_SUSPECTS = 13
EXPECTED_COVERAGE = {
    "DOXY": 619,
    "CHLA": 619,
    "BBP700": 619,
    "NITRATE": 126,
    "PH_IN_SITU_TOTAL": 316,
}

HARDENED_METADATA_KEYS = frozenset({
    "profile_id_sql",
    "float_id_sql",
    "cycle_number",
    "date",
    "has_bgc",
    "product_type",
    "bgc_parameters",
    "bgc_parameter_count",
})


def _resolve_chroma_dir(chroma_dir):
    if os.path.isabs(chroma_dir):
        return chroma_dir
    return os.path.join(BACKEND_DIR, chroma_dir)


def _engine_for_db(db_name):
    require_database_config()
    url = (
        f"mysql+mysqlconnector://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}"
        f"@{DB_HOST}:{DB_PORT}/{db_name}"
    )
    return create_engine(url)


def collect_netcdf_paths(data_root):
    """Return sorted list of ``.nc`` files under ``data_root``."""
    if not os.path.isdir(data_root):
        raise FileNotFoundError(f"BGC data directory does not exist: {data_root}")
    paths = []
    for root, _dirs, filenames in os.walk(data_root):
        for filename in filenames:
            if filename.lower().endswith(".nc"):
                paths.append(os.path.join(root, filename))
    return sorted(paths)


def compute_sql_fingerprint(engine):
    """Compute non-mutating row-count and payload-length fingerprint."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) AS rows_total,
                   COALESCE(MAX(profile_id), 0) AS max_profile_id,
                   COALESCE(SUM(CRC32(CONCAT_WS('|', profile_id, float_id,
                       cycle_number, profile_time, latitude, longitude,
                       direction))), 0) AS identity_hash,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(pressure AS CHAR)), 0)), 0) AS pres,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(temperature AS CHAR)), 0)), 0) AS temp,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(salinity AS CHAR)), 0)), 0) AS psal,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(bgc_params AS CHAR)), 0)), 0) AS bgc
            FROM argo_profiles
        """)).mappings().fetchone()
        floats_total = int(conn.execute(
            text("SELECT COUNT(*) FROM argo_floats")
        ).scalar() or 0)
        max_float_id = int(conn.execute(
            text("SELECT COALESCE(MAX(float_id), 0) FROM argo_floats")
        ).scalar() or 0)
    return {
        "rows_total": int(row["rows_total"]),
        "floats_total": floats_total,
        "max_profile_id": int(row["max_profile_id"]),
        "max_float_id": max_float_id,
        "identity_hash": str(row["identity_hash"]),
        "pres": str(row["pres"]),
        "temp": str(row["temp"]),
        "psal": str(row["psal"]),
        "bgc": str(row["bgc"]),
    }


def inspect_target_schema(engine):
    """Verify target MySQL schema supports V4 BGC columns, identity key, and FK."""
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.mysql_engine = engine
    manager._cached_profile_cols = None
    manager._cached_profile_unique_keys = DatabaseManager._UNCACHED

    columns = manager._get_profile_table_columns()
    unique_keys = manager._get_profile_unique_keys()

    missing_columns = sorted(DatabaseManager.REQUIRED_PROFILE_COLUMNS - columns)
    has_logical_uk = DatabaseManager.PROFILE_IDENTITY_KEY in unique_keys
    identity_cols = set(DatabaseManager.PROFILE_IDENTITY_KEY)
    narrower_uk = [
        list(key) for key in unique_keys
        if key != DatabaseManager.PROFILE_IDENTITY_KEY
        and not identity_cols <= set(key)
    ]

    with engine.connect() as conn:
        fk_rows = conn.execute(text("""
            SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
                   REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'argo_profiles'
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """)).mappings().fetchall()

    fks = [
        {
            "constraint": row["CONSTRAINT_NAME"],
            "column": row["COLUMN_NAME"],
            "references": f"{row['REFERENCED_TABLE_NAME']}({row['REFERENCED_COLUMN_NAME']})",
        }
        for row in fk_rows
    ]
    has_float_fk = any(
        fk["column"] == "float_id"
        and fk["references"] == "argo_floats(float_id)"
        for fk in fks
    )

    ok = (
        not missing_columns
        and has_logical_uk
        and not narrower_uk
        and has_float_fk
    )
    return {
        "ok": ok,
        "column_count": len(columns),
        "missing_required_columns": missing_columns,
        "unique_keys": [list(k) for k in unique_keys],
        "has_logical_identity_key": has_logical_uk,
        "narrower_unique_keys": narrower_uk,
        "foreign_keys": fks,
        "has_float_fk": has_float_fk,
    }


def inspect_development_state(engine, chroma_dir, collection_name):
    """Read-only snapshot of existing MySQL rows and ChromaDB vectors."""
    with engine.connect() as conn:
        float_rows = conn.execute(text("""
            SELECT float_id, wmo_number, project_name, platform_type
            FROM argo_floats
            ORDER BY wmo_number
        """)).mappings().fetchall()
        profile_count = int(conn.execute(
            text("SELECT COUNT(*) FROM argo_profiles")
        ).scalar() or 0)
        bgc_count = int(conn.execute(
            text("SELECT COUNT(*) FROM argo_profiles WHERE bgc_params IS NOT NULL")
        ).scalar() or 0)
        dir_rows = conn.execute(text("""
            SELECT direction, COUNT(*) AS n
            FROM argo_profiles
            GROUP BY direction
        """)).fetchall()
        identity_rows = conn.execute(text("""
            SELECT p.profile_id, f.wmo_number, p.cycle_number,
                   p.direction, p.profile_time
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
        """)).fetchall()

    sql_fp = compute_sql_fingerprint(engine)
    dev_floats = {int(r["wmo_number"]): int(r["float_id"]) for r in float_rows}
    dev_identities = {
        (int(r[1]), int(r[2]), str(r[3]), r[4]): int(r[0])
        for r in identity_rows
    }
    sql_vector_ids = {f"profile_{int(r[0])}" for r in identity_rows}

    resolved_chroma = _resolve_chroma_dir(chroma_dir)
    if not os.path.isdir(resolved_chroma):
        raise FileNotFoundError(f"ChromaDB directory not found: {resolved_chroma}")

    client = chromadb.PersistentClient(path=resolved_chroma)
    collection = client.get_collection(name=collection_name)
    vec_data = collection.get(include=["metadatas"])
    vec_ids = set(vec_data.get("ids") or [])
    metadatas = vec_data.get("metadatas") or []

    needs_backfill = 0
    up_to_date = 0
    for meta in metadatas:
        keys = set((meta or {}).keys())
        if HARDENED_METADATA_KEYS <= keys and "direction" in keys:
            up_to_date += 1
        else:
            needs_backfill += 1

    return {
        "mysql": {
            "floats_count": len(dev_floats),
            "profiles_count": profile_count,
            "bgc_profiles_count": bgc_count,
            "directions": {str(d): int(n) for d, n in dir_rows},
            "wmo_numbers": sorted(dev_floats.keys()),
            "fingerprint": sql_fp,
        },
        "chromadb": {
            "persist_dir": resolved_chroma,
            "collection": collection_name,
            "vector_count": len(vec_ids),
            "missing_vectors_for_sql_rows": len(sql_vector_ids - vec_ids),
            "extra_vectors_without_sql_row": len(vec_ids - sql_vector_ids),
            "vectors_up_to_date_metadata": up_to_date,
            "vectors_needing_backfill": needs_backfill,
        },
        "_dev_floats": dev_floats,
        "_dev_identities": dev_identities,
    }


def _read_netcdf_pres_and_bgc_params(path, profile_idx, include_bgc=False):
    """Lightweight read-only extraction of PRES values and BGC param names."""
    with netCDF4.Dataset(path, "r") as ds:
        pres = None
        if "PRES" in ds.variables:
            raw_pres = ds.variables["PRES"][profile_idx]
            arr = np.ma.filled(np.asarray(raw_pres), np.nan).astype("float64").ravel()
            pres = [
                None if (np.isnan(v) or v in (99999.0, 60000.0, -99999.0))
                else round(float(v), 4)
                for v in arr
            ]
        if not include_bgc:
            return pres, []

        bgc_names = []
        if "STATION_PARAMETERS" in ds.variables:
            raw_sp = np.ma.filled(ds.variables["STATION_PARAMETERS"][:], b" ")
            row = raw_sp[profile_idx] if raw_sp.ndim > 1 else raw_sp
            if row.ndim == 1 and row.dtype.kind in ("S", "a") and len(row) > 0 and len(row[0]) == 1:
                row = row.reshape(1, -1)
            for item in row:
                if isinstance(item, np.ndarray):
                    flat = np.ravel(item)
                    if all(isinstance(c, (bytes, np.bytes_)) for c in flat):
                        text_val = b"".join(bytes(c) for c in flat).decode("utf-8", "replace")
                    else:
                        text_val = "".join(str(c) for c in flat)
                elif isinstance(item, (bytes, np.bytes_)):
                    text_val = item.decode("utf-8", "replace")
                else:
                    text_val = str(item)
                name = text_val.strip("\x00 \t\r\n").upper()
                if (
                    name
                    and name not in {"PRES", "TEMP", "PSAL"}
                    and not is_core_auxiliary_parameter(name)
                    and name in ds.variables
                    and name not in bgc_names
                ):
                    bgc_names.append(name)
        return pres, bgc_names


def inspect_corpus_plans(data_root, verify_grids=True):
    """Index the NetCDF corpus, build logical plans, and verify grids/BGC params."""
    paths = collect_netcdf_paths(data_root)
    records = []
    index_errors = []
    for path in paths:
        try:
            records.extend(index_file(path))
        except Exception as exc:
            index_errors.append({"file": os.path.basename(path), "error": str(exc)})

    usable = [r for r in records if r["float"] is not None and r["cycle"] is not None]
    unusable_count = len(records) - len(usable)
    groups = group_by_cycle(usable)

    plans = []
    seen_identities = set()
    duplicate_planned_identities = []
    match_counts = {"matched": 0, "core_only": 0, "bgc_only": 0}
    direction_counts = {}
    grid_counts = {}
    parameter_coverage = {}
    suspect_profiles = []
    corpus_wmos = set()

    for (wmo_str, cycle_num), grp in sorted(groups.items()):
        wmo = int(wmo_str)
        corpus_wmos.add(wmo)
        for plan in plan_group(grp["core"], grp["bgc"]):
            direction = plan["direction"] or UNKNOWN_DIRECTION
            identity = (wmo, int(cycle_num), direction, plan["profile_time"])
            if identity in seen_identities:
                duplicate_planned_identities.append(
                    [wmo, int(cycle_num), direction, str(plan["profile_time"])]
                )
            seen_identities.add(identity)

            match_counts[plan["match"]] = match_counts.get(plan["match"], 0) + 1
            direction_counts[direction] = direction_counts.get(direction, 0) + 1

            grid_status = None
            bgc_names = []
            if verify_grids:
                core_pres = None
                if plan["core"] is not None:
                    core_pres, _ = _read_netcdf_pres_and_bgc_params(
                        plan["core"]["path"], plan["core"]["index"], include_bgc=False
                    )
                if plan["bgc"] is not None:
                    bgc_pres, bgc_names = _read_netcdf_pres_and_bgc_params(
                        plan["bgc"]["path"], plan["bgc"]["index"], include_bgc=True
                    )
                    grid_status, detail = classify_pressure_grid(
                        bgc_pres, core_pres if plan["core"] is not None else None
                    )
                    grid_counts[grid_status] = grid_counts.get(grid_status, 0) + 1
                    if grid_status == GRID_PRES_SCALE_SUSPECT:
                        suspect_profiles.append({
                            "wmo_number": wmo,
                            "cycle_number": int(cycle_num),
                            "direction": direction,
                            "bgc_file": plan["bgc"]["file"],
                            "core_file": plan["core"]["file"] if plan["core"] else None,
                            "median_ratio": detail.get("median_ratio"),
                        })
                    for param in bgc_names:
                        parameter_coverage[param] = parameter_coverage.get(param, 0) + 1

            plans.append({
                "identity": identity,
                "match": plan["match"],
                "grid_status": grid_status,
                "bgc_parameters": sorted(bgc_names),
            })

    return {
        "files_scanned": len(paths),
        "index_errors": index_errors,
        "unusable_records": unusable_count,
        "groups_count": len(groups),
        "planned_profiles_count": len(plans),
        "duplicate_planned_identities": duplicate_planned_identities,
        "match_counts": match_counts,
        "direction_counts": direction_counts,
        "grid_counts": grid_counts,
        "suspect_profiles": suspect_profiles,
        "parameter_coverage": parameter_coverage,
        "corpus_wmos": sorted(corpus_wmos),
        "_paths": paths,
        "_plans": plans,
    }


def analyze_promotion_collisions(plans_info, dev_state):
    """Cross-check planned identities and float WMOs against development DB."""
    dev_floats = dev_state["_dev_floats"]
    dev_identities = dev_state["_dev_identities"]
    dev_wmo_cycles = {(k[0], k[1]) for k in dev_identities}

    corpus_wmos = plans_info["corpus_wmos"]
    existing_wmos = [w for w in corpus_wmos if w in dev_floats]
    missing_wmos = [w for w in corpus_wmos if w not in dev_floats]

    exact_collisions = []
    wmo_cycle_overlaps = []
    eligible_count = 0

    for item in plans_info["_plans"]:
        ident = item["identity"]
        if ident in dev_identities:
            exact_collisions.append({
                "wmo_number": ident[0],
                "cycle_number": ident[1],
                "direction": ident[2],
                "profile_time": str(ident[3]),
                "existing_profile_id": dev_identities[ident],
            })
        else:
            eligible_count += 1
        if (ident[0], ident[1]) in dev_wmo_cycles:
            wmo_cycle_overlaps.append([ident[0], ident[1]])

    return {
        "required_wmos": corpus_wmos,
        "existing_floats_in_dev": existing_wmos,
        "new_floats_to_create": missing_wmos,
        "exact_identity_collisions_count": len(exact_collisions),
        "exact_identity_collisions": exact_collisions[:20],
        "wmo_cycle_overlap_count": len(wmo_cycle_overlaps),
        "eligible_profiles_to_insert": eligible_count,
        "existing_profiles_preserved": dev_state["mysql"]["profiles_count"],
        "projected_total_profiles_after_apply": (
            dev_state["mysql"]["profiles_count"] + eligible_count
        ),
        "projected_total_floats_after_apply": (
            dev_state["mysql"]["floats_count"] + len(missing_wmos)
        ),
    }


def verify_vector_metadata_contract():
    """Verify ``_build_profile_metadata`` emits scalar V4/hardening keys."""
    manager = DatabaseManager.__new__(DatabaseManager)
    sample_time = datetime(2024, 12, 1, 13, 28, 0)
    core_meta = manager._build_profile_metadata(
        profile_id_db=1, float_id_db=1900121, cycle=44, time=sample_time,
        lat=-10.5, lon=48.909, bgc_keys=None, direction="A",
    )
    bgc_meta = manager._build_profile_metadata(
        profile_id_db=1385, float_id_db=6990679, cycle=1, time=sample_time,
        lat=-11.5607, lon=67.7328,
        bgc_keys=["DOXY", "CHLA", "BBP700", "CHLA_FLUORESCENCE"],
        direction="A",
    )
    scalar_types = (bool, int, float, str)
    core_scalars_ok = all(isinstance(v, scalar_types) for v in core_meta.values())
    bgc_scalars_ok = all(isinstance(v, scalar_types) for v in bgc_meta.values())
    return {
        "ok": (
            core_scalars_ok
            and bgc_scalars_ok
            and core_meta["has_bgc"] is False
            and core_meta["product_type"] == DatabaseManager.PRODUCT_TYPE_CORE
            and bgc_meta["has_bgc"] is True
            and bgc_meta["product_type"] == DatabaseManager.PRODUCT_TYPE_CORE_BGC
            and bgc_meta["bgc_parameters"] == "BBP700,CHLA,CHLA_FLUORESCENCE,DOXY"
            and bgc_meta["bgc_parameter_count"] == 4
        ),
        "core_sample_metadata": core_meta,
        "bgc_sample_metadata": bgc_meta,
    }


def verify_backups(mysql_backup_path, chroma_backup_dir,
                   live_chroma_dir, expected_collection,
                   expected_min_vectors=1):
    """Verify real backup artifacts exist, are non-empty, and contain collection data."""
    if not mysql_backup_path:
        raise RuntimeError(
            "Missing --mysql-backup <path>. Create a MySQL dump before --apply."
        )
    if not os.path.isfile(mysql_backup_path):
        raise RuntimeError(
            f"MySQL backup file does not exist: {mysql_backup_path}"
        )
    mysql_size = os.path.getsize(mysql_backup_path)
    if mysql_size <= 0:
        raise RuntimeError(
            f"MySQL backup file is empty (0 bytes): {mysql_backup_path}"
        )

    if not chroma_backup_dir:
        raise RuntimeError(
            "Missing --chroma-backup <dir>. Copy ChromaDB storage before --apply."
        )
    if not os.path.isdir(chroma_backup_dir):
        raise RuntimeError(
            f"ChromaDB backup directory does not exist: {chroma_backup_dir}"
        )
    if os.path.abspath(chroma_backup_dir) == os.path.abspath(live_chroma_dir):
        raise RuntimeError(
            "ChromaDB backup directory cannot be the live ChromaDB directory."
        )
    sqlite_file = os.path.join(chroma_backup_dir, "chroma.sqlite3")
    if not os.path.isfile(sqlite_file) or os.path.getsize(sqlite_file) <= 0:
        raise RuntimeError(
            f"ChromaDB backup is missing non-empty chroma.sqlite3: {chroma_backup_dir}"
        )

    backup_client = chromadb.PersistentClient(path=chroma_backup_dir)
    collection_names = [col.name for col in backup_client.list_collections()]
    if expected_collection not in collection_names:
        raise RuntimeError(
            f"ChromaDB backup does not contain collection '{expected_collection}' "
            f"(found {collection_names})"
        )
    backup_count = backup_client.get_collection(expected_collection).count()
    if backup_count < expected_min_vectors:
        raise RuntimeError(
            f"ChromaDB backup collection '{expected_collection}' has {backup_count} "
            f"vectors; expected at least {expected_min_vectors}"
        )

    return {
        "mysql_backup_path": os.path.abspath(mysql_backup_path),
        "mysql_backup_bytes": mysql_size,
        "chroma_backup_dir": os.path.abspath(chroma_backup_dir),
        "chroma_backup_collection": expected_collection,
        "chroma_backup_vectors": backup_count,
    }


def verify_target_and_baseline(engine, chroma_dir, collection_name,
                               expected_db=EXPECTED_TARGET_DB,
                               expected_collection=EXPECTED_TARGET_COLLECTION,
                               dry_run_report_path=None,
                               require_unmodified_baseline=False):
    """Verify target DB/collection names and compare live state against dry-run report."""
    if DB_NAME != expected_db:
        raise RuntimeError(
            f"Target database '{DB_NAME}' does not match expected '{expected_db}'."
        )
    if collection_name != expected_collection:
        raise RuntimeError(
            f"Target collection '{collection_name}' does not match expected "
            f"'{expected_collection}'."
        )

    live_state = inspect_development_state(engine, chroma_dir, collection_name)

    if dry_run_report_path and os.path.isfile(dry_run_report_path):
        with open(dry_run_report_path, encoding="utf-8") as handle:
            baseline = json.load(handle)
        base_db = baseline.get("target_database")
        base_col = baseline.get("target_chroma_collection")
        if base_db and base_db != DB_NAME:
            raise RuntimeError(
                f"Dry-run report target_database '{base_db}' != '{DB_NAME}'."
            )
        if base_col and base_col != collection_name:
            raise RuntimeError(
                f"Dry-run report collection '{base_col}' != '{collection_name}'."
            )
        if require_unmodified_baseline:
            base_fp = baseline.get("development_baseline", {}).get("mysql", {}).get("fingerprint")
            live_fp = live_state["mysql"]["fingerprint"]
            if base_fp and live_fp != base_fp:
                raise RuntimeError(
                    f"Live MySQL baseline fingerprint {live_fp} does not match "
                    f"dry-run report baseline {base_fp}."
                )
            base_vecs = baseline.get("development_baseline", {}).get("chromadb", {}).get("vector_count")
            live_vecs = live_state["chromadb"]["vector_count"]
            if base_vecs is not None and live_vecs != base_vecs:
                raise RuntimeError(
                    f"Live ChromaDB count {live_vecs} != dry-run baseline {base_vecs}."
                )

    return live_state


def run_dry_run(data_root=DEFAULT_DATA_ROOT, report_path=DEFAULT_REPORT_PATH,
                verify_grids=True, expected_db=EXPECTED_TARGET_DB,
                expected_collection=EXPECTED_TARGET_COLLECTION):
    """Execute full read-only pre-promotion dry-run checks."""
    engine = _engine_for_db(DB_NAME)
    try:
        schema = inspect_target_schema(engine)
        dev_state = inspect_development_state(
            engine, CHROMA_PERSIST_DIR, CHROMA_COLLECTION_NAME
        )
    finally:
        engine.dispose()

    plans_info = inspect_corpus_plans(data_root, verify_grids=verify_grids)
    collisions = analyze_promotion_collisions(plans_info, dev_state)
    metadata_contract = verify_vector_metadata_contract()

    checks = {
        "target_db_matches_expected": DB_NAME == expected_db,
        "target_collection_matches_expected": CHROMA_COLLECTION_NAME == expected_collection,
        "target_schema_v4_ready": schema["ok"],
        "no_missing_vectors_in_dev": (
            dev_state["chromadb"]["missing_vectors_for_sql_rows"] == 0
        ),
        "no_orphan_vectors_in_dev": (
            dev_state["chromadb"]["extra_vectors_without_sql_row"] == 0
        ),
        "corpus_file_count_1264": (
            plans_info["files_scanned"] == EXPECTED_CORPUS_FILES
        ),
        "corpus_index_errors_zero": len(plans_info["index_errors"]) == 0,
        "corpus_groups_626": plans_info["groups_count"] == EXPECTED_GROUPS,
        "corpus_planned_632": (
            plans_info["planned_profiles_count"] == EXPECTED_PROFILES
        ),
        "no_duplicate_planned_identities": (
            len(plans_info["duplicate_planned_identities"]) == 0
        ),
        "all_632_matched": (
            plans_info["match_counts"].get("matched") == EXPECTED_PROFILES
        ),
        "zero_identity_collisions_with_dev": (
            collisions["exact_identity_collisions_count"] == 0
        ),
        "metadata_contract_valid": metadata_contract["ok"],
    }
    if verify_grids:
        checks["pres_scale_suspect_13"] = (
            plans_info["grid_counts"].get(GRID_PRES_SCALE_SUSPECT, 0)
            == EXPECTED_SUSPECTS
        )
        checks["parameter_coverage_matches_expected"] = all(
            plans_info["parameter_coverage"].get(k) == v
            for k, v in EXPECTED_COVERAGE.items()
        )

    dev_state_public = {
        k: v for k, v in dev_state.items() if not k.startswith("_")
    }
    plans_public = {
        k: v for k, v in plans_info.items() if not k.startswith("_")
    }

    report = {
        "mode": "dry_run",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_database": DB_NAME,
        "target_chroma_dir": _resolve_chroma_dir(CHROMA_PERSIST_DIR),
        "target_chroma_collection": CHROMA_COLLECTION_NAME,
        "source_corpus_dir": os.path.abspath(data_root),
        "schema": schema,
        "development_baseline": dev_state_public,
        "corpus_plan": plans_public,
        "promotion_collisions_and_fks": collisions,
        "vector_metadata_contract": metadata_contract,
        "backfill_plan": {
            "existing_vectors_to_backfill": dev_state["chromadb"]["vectors_needing_backfill"],
            "method": "DatabaseManager.sync_profile_vector(profile_id)",
            "re_embeds_documents": True,
            "reason": (
                "Existing dev vectors use the legacy document format (no "
                "Profile Direction or Bio-geochemical Parameters section, and "
                "fractional JULD timestamps) and lack V4/hardening metadata keys; "
                "sync_profile_vector calls collection.update with _embed_document."
            ),
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }

    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)

    return report


def _normalize_profile_time(value):
    return str(value).split(".")[0]


def _snapshot_ownership_state(engine):
    with engine.connect() as conn:
        floats = {
            int(r[0]): int(r[1])
            for r in conn.execute(
                text("SELECT wmo_number, float_id FROM argo_floats")
            ).fetchall()
        }
        profiles = {
            (int(r[1]), int(r[2]), str(r[3]), _normalize_profile_time(r[4])): {
                "profile_id": int(r[0]),
                "float_id": int(r[5]),
                "wmo_number": int(r[1]),
                "cycle_number": int(r[2]),
                "direction": str(r[3]),
                "profile_time": _normalize_profile_time(r[4]),
            }
            for r in conn.execute(text("""
                SELECT p.profile_id, f.wmo_number, p.cycle_number,
                       p.direction, p.profile_time, p.float_id
                FROM argo_profiles p
                JOIN argo_floats f ON f.float_id = p.float_id
            """)).fetchall()
        }
    return floats, profiles


def run_stage_a_ingest(db_manager, paths, expected_total_profiles=None,
                       manifest_path=DEFAULT_STAGE_A_MANIFEST,
                       allowed_identities=None,
                       target_db=DB_NAME,
                       target_collection=CHROMA_COLLECTION_NAME):
    """Stage A: Ingest BGC corpus profiles and record exact ownership manifest."""
    pre_floats, pre_profiles = _snapshot_ownership_state(db_manager.mysql_engine)
    allowed_set = (
        {
            (int(ident[0]), int(ident[1]), str(ident[2]), _normalize_profile_time(ident[3]))
            for ident in allowed_identities
        }
        if allowed_identities is not None
        else None
    )

    processor = ArgoDataProcessor(db_manager)
    ingest_error = None
    ingest_summary = None
    try:
        ingest_summary = processor.process_paths(paths, progress=True)
        if ingest_summary["errors"]:
            ingest_error = RuntimeError(
                f"Stage A ingestion reported {len(ingest_summary['errors'])} "
                f"error(s): {ingest_summary['errors'][:5]}"
            )
    except Exception as exc:
        ingest_error = exc

    post_floats, post_profiles = _snapshot_ownership_state(db_manager.mysql_engine)
    inserted_profiles = []
    for ident_key, info in sorted(
        post_profiles.items(), key=lambda item: item[1]["profile_id"]
    ):
        if ident_key in pre_profiles:
            continue
        if allowed_set is not None and ident_key not in allowed_set:
            continue
        inserted_profiles.append(info)

    promoted_wmos = sorted({p["wmo_number"] for p in inserted_profiles})
    inserted_floats = [
        {"wmo_number": wmo, "float_id": post_floats[wmo]}
        for wmo in sorted(post_floats)
        if wmo not in pre_floats
        and (allowed_set is None or wmo in {k[0] for k in allowed_set})
    ]
    inserted_vector_ids = [f"profile_{p['profile_id']}" for p in inserted_profiles]

    manifest = {
        "stage": "ingest",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_database": target_db,
        "target_chroma_collection": target_collection,
        "pre_existing_profiles_count": len(pre_profiles),
        "pre_existing_floats_count": len(pre_floats),
        "sql_profiles_total": len(post_profiles),
        "sql_floats_total": len(post_floats),
        "promoted_wmos": promoted_wmos,
        "inserted_floats": inserted_floats,
        "inserted_profiles": inserted_profiles,
        "inserted_vector_ids": inserted_vector_ids,
        "ingestion_summary": ingest_summary,
    }
    if manifest_path:
        os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, default=str)

    if ingest_error is not None:
        raise ingest_error

    with db_manager.mysql_engine.connect() as conn:
        dup_count = len(conn.execute(text("""
            SELECT float_id, cycle_number, direction, profile_time, COUNT(*) n
            FROM argo_profiles
            GROUP BY float_id, cycle_number, direction, profile_time
            HAVING n > 1
        """)).fetchall())

    sql_ids = {info["profile_id"] for info in post_profiles.values()}
    expected_vec_ids = {f"profile_{pid}" for pid in sql_ids}
    actual_vec_ids = set(db_manager.chroma_collection.get(include=[])["ids"])
    missing_vecs = sorted(expected_vec_ids - actual_vec_ids)
    extra_vecs = sorted(actual_vec_ids - expected_vec_ids)

    if dup_count > 0:
        raise RuntimeError(f"Stage A found {dup_count} duplicate logical identities.")
    if missing_vecs or extra_vecs:
        raise RuntimeError(
            f"Stage A SQL/vector parity mismatch: missing={len(missing_vecs)}, "
            f"extra={len(extra_vecs)}"
        )
    if expected_total_profiles is not None and len(sql_ids) != expected_total_profiles:
        raise RuntimeError(
            f"Stage A expected {expected_total_profiles} SQL rows, found {len(sql_ids)}."
        )

    manifest["chroma_vectors_total"] = len(actual_vec_ids)
    if manifest_path:
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, default=str)

    return manifest


def rollback_stage_a(db_manager, manifest_path=DEFAULT_STAGE_A_MANIFEST,
                     expected_db=EXPECTED_TARGET_DB,
                     expected_collection=EXPECTED_TARGET_COLLECTION,
                     dry_run=True):
    """Verify exact promotion ownership and roll back Stage A records."""
    if not os.path.isfile(manifest_path):
        raise RuntimeError(f"Stage A manifest not found: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    if manifest.get("target_database") != expected_db:
        raise RuntimeError(
            f"Manifest target_database '{manifest.get('target_database')}' "
            f"!= expected '{expected_db}'."
        )
    if manifest.get("target_chroma_collection") != expected_collection:
        raise RuntimeError(
            f"Manifest target_chroma_collection '{manifest.get('target_chroma_collection')}' "
            f"!= expected '{expected_collection}'."
        )

    recorded_profiles = manifest.get("inserted_profiles") or []
    recorded_floats = manifest.get("inserted_floats") or []
    recorded_vec_ids = manifest.get("inserted_vector_ids") or []
    expected_vec_ids = {f"profile_{int(p['profile_id'])}" for p in recorded_profiles}
    if set(recorded_vec_ids) != expected_vec_ids:
        raise RuntimeError(
            "Manifest inserted_vector_ids do not match inserted_profiles."
        )

    with db_manager.mysql_engine.connect() as conn:
        live_profiles = {
            int(r[0]): {
                "profile_id": int(r[0]),
                "float_id": int(r[1]),
                "wmo_number": int(r[2]),
                "cycle_number": int(r[3]),
                "direction": str(r[4]),
                "profile_time": _normalize_profile_time(r[5]),
            }
            for r in conn.execute(text("""
                SELECT p.profile_id, p.float_id, f.wmo_number,
                       p.cycle_number, p.direction, p.profile_time
                FROM argo_profiles p
                JOIN argo_floats f ON f.float_id = p.float_id
            """)).fetchall()
        }
        live_floats = {
            int(r[0]): int(r[1])
            for r in conn.execute(
                text("SELECT float_id, wmo_number FROM argo_floats")
            ).fetchall()
        }

    recorded_pid_set = {int(p["profile_id"]) for p in recorded_profiles}
    recorded_fid_set = {int(f["float_id"]) for f in recorded_floats}

    # Verify every recorded profile still matches its manifest identity.
    profiles_to_delete = []
    for rec in recorded_profiles:
        pid = int(rec["profile_id"])
        live = live_profiles.get(pid)
        if live is None:
            continue
        expected_tuple = (
            int(rec["float_id"]),
            int(rec["wmo_number"]),
            int(rec["cycle_number"]),
            str(rec["direction"]),
            _normalize_profile_time(rec["profile_time"]),
        )
        live_tuple = (
            live["float_id"],
            live["wmo_number"],
            live["cycle_number"],
            live["direction"],
            live["profile_time"],
        )
        if live_tuple != expected_tuple:
            raise RuntimeError(
                f"Ownership mismatch on profile_id={pid}: expected {expected_tuple}, "
                f"found {live_tuple}. Refusing rollback."
            )
        profiles_to_delete.append(pid)

    # Verify every recorded float matches its WMO and has no unexpected child profiles.
    floats_to_delete = []
    for rec in recorded_floats:
        fid = int(rec["float_id"])
        wmo = int(rec["wmo_number"])
        if fid not in live_floats:
            continue
        if live_floats[fid] != wmo:
            raise RuntimeError(
                f"Ownership mismatch on float_id={fid}: expected WMO {wmo}, "
                f"found {live_floats[fid]}. Refusing rollback."
            )
        unexpected_children = [
            pid for pid, row in live_profiles.items()
            if row["float_id"] == fid and pid not in recorded_pid_set
        ]
        if unexpected_children:
            raise RuntimeError(
                f"Unexpected profile(s) {unexpected_children[:5]} reference "
                f"promoted float_id={fid}. Refusing rollback."
            )
        floats_to_delete.append(fid)

    # Verify ChromaDB vector ownership for existing target vectors.
    existing_vecs = db_manager.chroma_collection.get(
        ids=list(expected_vec_ids), include=["metadatas"]
    ) if expected_vec_ids else {"ids": [], "metadatas": []}
    vectors_to_delete = []
    for vid, meta in zip(
        existing_vecs.get("ids") or [], existing_vecs.get("metadatas") or []
    ):
        expected_pid = int(vid.split("_", 1)[1])
        if meta and int(meta.get("profile_id_sql", expected_pid)) != expected_pid:
            raise RuntimeError(
                f"ChromaDB vector {vid} metadata profile_id_sql mismatch. "
                "Refusing rollback."
            )
        vectors_to_delete.append(vid)

    summary = {
        "mode": "rollback_dry_run" if dry_run else "rollback_applied",
        "manifest_path": os.path.abspath(manifest_path),
        "ownership_verified": True,
        "profiles_to_delete": len(profiles_to_delete),
        "floats_to_delete": len(floats_to_delete),
        "vectors_to_delete": len(vectors_to_delete),
        "deleted_profile_ids_sample": profiles_to_delete[:10],
        "deleted_float_ids": floats_to_delete,
    }
    if dry_run:
        return summary

    if vectors_to_delete:
        db_manager.chroma_collection.delete(ids=vectors_to_delete)

    with db_manager.mysql_engine.begin() as conn:
        for pid in profiles_to_delete:
            conn.execute(
                text("DELETE FROM argo_profiles WHERE profile_id = :pid"),
                {"pid": pid},
            )
        for fid in floats_to_delete:
            remaining = int(conn.execute(
                text("SELECT COUNT(*) FROM argo_profiles WHERE float_id = :fid"),
                {"fid": fid},
            ).scalar() or 0)
            if remaining == 0:
                conn.execute(
                    text("DELETE FROM argo_floats WHERE float_id = :fid"),
                    {"fid": fid},
                )

    return summary


def run_stage_b_backfill(db_manager):
    """Stage B: Resumable, idempotent backfill via ``sync_profile_vector``."""
    with db_manager.mysql_engine.connect() as conn:
        pids = [
            int(r[0]) for r in conn.execute(
                text("SELECT profile_id FROM argo_profiles ORDER BY profile_id")
            ).fetchall()
        ]

    expected_vec_ids = {f"profile_{pid}" for pid in pids}
    pre_vec_ids = set(db_manager.chroma_collection.get(include=[])["ids"])
    missing_before = sorted(expected_vec_ids - pre_vec_ids)

    counts = {"updated": 0, "unchanged": 0, "recreated": 0, "failed": 0}
    failures = []
    for pid in pids:
        try:
            status = db_manager.sync_profile_vector(pid)
            counts[status] = counts.get(status, 0) + 1
        except Exception as exc:
            counts["failed"] += 1
            failures.append({"profile_id": pid, "error": _redact(str(exc))})

    post_vec_ids = set(db_manager.chroma_collection.get(include=[])["ids"])
    missing_after = sorted(expected_vec_ids - post_vec_ids)

    return {
        "stage": "backfill",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "profiles_scanned": len(pids),
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "recreated": counts["recreated"],
        "failed": counts["failed"],
        "missing_before": len(missing_before),
        "missing_after": len(missing_after),
        "failures": failures,
    }


def run_apply(stage="all", data_root=DEFAULT_DATA_ROOT,
              mysql_backup_path=None, chroma_backup_dir=None,
              report_path=DEFAULT_REPORT_PATH,
              expected_db=EXPECTED_TARGET_DB,
              expected_collection=EXPECTED_TARGET_COLLECTION,
              resume=False):
    """Execute Stage A, Stage B, or both after verifying real backups & baseline."""
    live_chroma = _resolve_chroma_dir(CHROMA_PERSIST_DIR)
    engine = _engine_for_db(DB_NAME)
    try:
        # On a fresh Stage A or combined run, verify baseline matches dry-run;
        # on --resume or Stage B alone, allow already-promoted corpus rows.
        live_state = verify_target_and_baseline(
            engine,
            CHROMA_PERSIST_DIR,
            CHROMA_COLLECTION_NAME,
            expected_db=expected_db,
            expected_collection=expected_collection,
            dry_run_report_path=report_path,
            require_unmodified_baseline=(stage in ("ingest", "all") and not resume),
        )
    finally:
        engine.dispose()

    backup_info = verify_backups(
        mysql_backup_path=mysql_backup_path,
        chroma_backup_dir=chroma_backup_dir,
        live_chroma_dir=live_chroma,
        expected_collection=expected_collection,
        expected_min_vectors=min(1384, live_state["chromadb"]["vector_count"]),
    )

    expected_total = None
    allowed_identities = None
    if stage in ("ingest", "all"):
        dry_report = run_dry_run(
            data_root=data_root,
            report_path=report_path if not resume else None,
            verify_grids=True,
            expected_db=expected_db,
            expected_collection=expected_collection,
        )
        allowed_skip = {"zero_identity_collisions_with_dev"} if resume else set()
        failed = [
            k for k, v in dry_report["checks"].items()
            if not v and k not in allowed_skip
        ]
        if failed:
            raise RuntimeError(
                f"Pre-flight dry-run failed checks: {failed}. Aborting --apply."
            )
        expected_total = (
            live_state["mysql"]["profiles_count"]
            + dry_report["promotion_collisions_and_fks"]["eligible_profiles_to_insert"]
        )
        plans_info = inspect_corpus_plans(data_root, verify_grids=False)
        allowed_identities = [item["identity"] for item in plans_info["_plans"]]

    db_manager = DatabaseManager()
    try:
        results = {"mode": "apply", "stage": stage, "backup_verified": backup_info}
        if stage in ("ingest", "all"):
            paths = collect_netcdf_paths(data_root)
            results["stage_a"] = run_stage_a_ingest(
                db_manager,
                paths,
                expected_total_profiles=expected_total,
                allowed_identities=allowed_identities,
                target_db=DB_NAME,
                target_collection=CHROMA_COLLECTION_NAME,
            )
        if stage in ("backfill", "all"):
            results["stage_b"] = run_stage_b_backfill(db_manager)
            if results["stage_b"]["failed"] > 0:
                raise RuntimeError(
                    f"Stage B backfill had {results['stage_b']['failed']} "
                    f"failure(s): {results['stage_b']['failures'][:5]}"
                )
        return results
    finally:
        db_manager.mysql_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_ROOT,
        help="Path to the verified 1,264-file BGC NetCDF corpus.",
    )
    parser.add_argument(
        "--report-path",
        default=DEFAULT_REPORT_PATH,
        help="Path to write/read the JSON dry-run report.",
    )
    parser.add_argument(
        "--skip-grid-check",
        action="store_true",
        help="Skip per-file NetCDF pressure-grid inspection during dry-run.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute promotion stage(s) against development MySQL and ChromaDB.",
    )
    parser.add_argument(
        "--stage",
        choices=("ingest", "backfill", "all"),
        default="all",
        help="Stage to run with --apply: ingest (Stage A), backfill (Stage B), or all.",
    )
    parser.add_argument(
        "--mysql-backup",
        help="Path to verified non-empty pre-promotion MySQL dump file.",
    )
    parser.add_argument(
        "--chroma-backup",
        help="Path to verified pre-promotion ChromaDB backup directory.",
    )
    parser.add_argument(
        "--expected-db",
        default=EXPECTED_TARGET_DB,
        help=f"Expected target MySQL database name (default: {EXPECTED_TARGET_DB}).",
    )
    parser.add_argument(
        "--expected-collection",
        default=EXPECTED_TARGET_COLLECTION,
        help=f"Expected target ChromaDB collection (default: {EXPECTED_TARGET_COLLECTION}).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Allow resuming an interrupted Stage A run where some corpus rows already exist.",
    )
    parser.add_argument(
        "--rollback-stage-a",
        action="store_true",
        help="Verify ownership and roll back Stage A using the Stage A manifest (dry-run unless --apply is set).",
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_STAGE_A_MANIFEST,
        help="Path to the Stage A ownership manifest JSON.",
    )
    args = parser.parse_args()

    try:
        if args.rollback_stage_a:
            db_manager = DatabaseManager()
            try:
                res = rollback_stage_a(
                    db_manager,
                    manifest_path=args.manifest,
                    expected_db=args.expected_db,
                    expected_collection=args.expected_collection,
                    dry_run=not args.apply,
                )
            finally:
                db_manager.mysql_engine.dispose()
            print(json.dumps(res, indent=2, default=str))
            return 0

        if args.apply:
            result = run_apply(
                stage=args.stage,
                data_root=args.data_dir,
                mysql_backup_path=args.mysql_backup,
                chroma_backup_dir=args.chroma_backup,
                report_path=args.report_path,
                expected_db=args.expected_db,
                expected_collection=args.expected_collection,
                resume=args.resume,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0

        report = run_dry_run(
            data_root=args.data_dir,
            report_path=args.report_path,
            verify_grids=not args.skip_grid_check,
            expected_db=args.expected_db,
            expected_collection=args.expected_collection,
        )
        print(f"Mode               : {report['mode']} (no writes performed)")
        print(f"Target DB          : {report['target_database']}")
        print(f"Target ChromaDB    : {report['target_chroma_collection']} ({report['target_chroma_dir']})")
        print(f"Schema V4 ready    : {report['schema']['ok']} ({report['schema']['column_count']} columns)")
        print(f"Dev baseline       : {report['development_baseline']['mysql']['profiles_count']} profiles, "
              f"{report['development_baseline']['mysql']['floats_count']} floats, "
              f"{report['development_baseline']['chromadb']['vector_count']} vectors")
        print(f"Dev SQL fingerprint: {report['development_baseline']['mysql']['fingerprint']}")
        print(f"Dev vectors needing backfill: {report['backfill_plan']['existing_vectors_to_backfill']}")
        print(f"Corpus planned     : {report['corpus_plan']['files_scanned']} files, "
              f"{report['corpus_plan']['groups_count']} groups, "
              f"{report['corpus_plan']['planned_profiles_count']} profiles")
        print(f"New floats needed  : {len(report['promotion_collisions_and_fks']['new_floats_to_create'])} "
              f"{report['promotion_collisions_and_fks']['new_floats_to_create']}")
        print(f"Identity collisions: {report['promotion_collisions_and_fks']['exact_identity_collisions_count']}")
        print(f"Eligible to insert : {report['promotion_collisions_and_fks']['eligible_profiles_to_insert']}")
        print(f"Checks             : {sum(1 for v in report['checks'].values() if v)}/{len(report['checks'])} passed")
        print(f"Report written     : {args.report_path}")
        return 0 if report["all_checks_passed"] else 1
    except Exception as exc:
        print(f"ERROR: {_redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
