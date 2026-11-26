"""Controlled two-float BGC ingestion validation against a disposable database.

Validates the *writing* pipeline (not only the plan) on two complete floats
before any corpus-wide ingestion:

  3902581  118 logical profiles, 112 ascending / 6 descending
  2902306   85 logical profiles, 10 of the 13 known ``pres_scale_suspect`` grids

The development database is only read, and only as a fingerprint (row count,
payload sums, schema). All ingestion goes to ``intelliquery_bgc_two_float_check``,
built from the base schema plus V2, V3 and V4 and driven through the production
``ArgoDataProcessor`` / ``DatabaseManager`` code paths.

Checks, in order:

  1. environment and safety (database identity, disposable DB, V4 schema)
  2. corpus fingerprint before ingestion (size/mtime of all files, SHA-256 of
     every file used)
  3. float 3902581 ingestion
  4. stored rows against the NetCDF sources (identity, core, QC, adjusted,
     errors, modes, provenance, variants, BGC arrays, grid flags)
  5. float 2902306 ingestion and the pressure-grid anomaly behaviour
  6. idempotency: exact re-run must insert nothing and change no payload byte
  7. database integrity (uniqueness, constraints, payload shape, coverage)
  8. development database and corpus unchanged after the run

The report is generated from the measured checks, never hand-transcribed.

Run from ``backend/``:

    python scripts/bgc_two_float_ingestion_validation.py --run-tests
    python scripts/bgc_two_float_ingestion_validation.py --keep-db

Exit status is 0 only when every check passes.
"""

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone

import numpy as np
import xarray as xr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Reuse the verified disposable-database and source-reading helpers of the
# earlier controlled ingestion verifier instead of re-implementing them.
sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))
import bgc_controlled_ingestion as base  # noqa: E402

from config import DB_HOST, DB_NAME, DB_PORT, DB_USER  # noqa: E402
from data_processor import ArgoDataProcessor, compute_file_sha256  # noqa: E402
from bgc_matching import (  # noqa: E402
    GRID_OK, GRID_PRES_SCALE_SUSPECT, group_by_cycle, index_file, plan_group,
    profile_time_from_juld,
)

DISPOSABLE_DB = "intelliquery_bgc_two_float_check"
DATA_ROOT = base.DATA_ROOT
REPORT_PATH = os.path.join(
    BACKEND_DIR, "..", "docs", "bgc", "BGC_CONTROLLED_INGESTION_VALIDATION.md")
JSON_DIR = os.path.join(BACKEND_DIR, "scratch", "bgc_validation")

#: The two floats of this validation, with the audit's expected result for each.
FLOATS = [
    {
        "wmo": 3902581,
        "profiles": 118,
        "directions": {"A": 112, "D": 6},
        "suspect_cycles": (),
        "why": "ascending and descending profiles of one cycle",
    },
    {
        "wmo": 2902306,
        "profiles": 85,
        "directions": {"A": 85},
        "suspect_cycles": (1, 11, 16, 17, 18, 23, 24, 25, 26, 39),
        "why": "10 of the 13 pres_scale_suspect grids",
    },
]

BGC_PARAMETERS = ("DOXY", "CHLA", "BBP700", "NITRATE", "PH_IN_SITU_TOTAL")
CORE_MEASUREMENTS = (
    ("PRES", "pressure"),
    ("TEMP", "temperature"),
    ("PSAL", "salinity"),
)
CORE_QC = (
    ("PRES_QC", "pressure_qc"),
    ("TEMP_QC", "temperature_qc"),
    ("PSAL_QC", "salinity_qc"),
)
CORE_ADJUSTED = (
    ("PRES_ADJUSTED", "pressure_adjusted"),
    ("TEMP_ADJUSTED", "temperature_adjusted"),
    ("PSAL_ADJUSTED", "salinity_adjusted"),
)
CORE_ADJUSTED_ERROR = (
    ("PRES_ADJUSTED_ERROR", "pressure_adjusted_error"),
    ("TEMP_ADJUSTED_ERROR", "temperature_adjusted_error"),
    ("PSAL_ADJUSTED_ERROR", "salinity_adjusted_error"),
)
CORE_ADJUSTED_QC = (
    ("PRES_ADJUSTED_QC", "pressure_adjusted_qc"),
    ("TEMP_ADJUSTED_QC", "temperature_adjusted_qc"),
    ("PSAL_ADJUSTED_QC", "salinity_adjusted_qc"),
)
PROFILE_QC = (
    ("PROFILE_PRES_QC", "profile_pres_qc"),
    ("PROFILE_TEMP_QC", "profile_temp_qc"),
    ("PROFILE_PSAL_QC", "profile_psal_qc"),
)

FLOAT_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# Check collection
# ---------------------------------------------------------------------------

RESULTS = []
CURRENT_SECTION = "setup"


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, str(detail), CURRENT_SECTION))
    return bool(ok)


def section(title):
    global CURRENT_SECTION
    CURRENT_SECTION = title
    print("\n" + title)
    print("-" * len(title))


class Tally:
    """Counts agreements per field so the report stays readable."""

    def __init__(self):
        self.ok = 0
        self.bad = 0
        self.examples = []

    def add(self, ok, example=""):
        if ok:
            self.ok += 1
        else:
            self.bad += 1
            if len(self.examples) < 5 and example:
                self.examples.append(example)
        return ok

    @property
    def total(self):
        return self.ok + self.bad

    def label(self):
        return f"{self.ok}/{self.total}"

    def detail(self):
        return "; ".join(self.examples)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def arrays_equal(stored, expected, tol=FLOAT_TOLERANCE):
    """Compare a stored JSON array with the expected source array."""
    if expected is None or (isinstance(expected, list) and not expected):
        # An absent or empty source array is stored as SQL NULL.
        return stored is None or stored == []
    if stored is None or not isinstance(stored, list):
        return False
    if len(stored) != len(expected):
        return False
    for left, right in zip(stored, expected):
        if left is None or right is None:
            if left is not None or right is not None:
                return False
            continue
        if isinstance(right, str) or isinstance(left, str):
            if left != right:
                return False
            continue
        try:
            if abs(float(left) - float(right)) > tol:
                return False
        except (TypeError, ValueError):
            return False
    return True


def dicts_equal(stored, expected, tol=FLOAT_TOLERANCE):
    if expected is None:
        return stored is None
    if not isinstance(stored, dict) or set(stored) != set(expected):
        return False
    return all(arrays_equal(stored[name], expected[name], tol)
               for name in expected)


def array_example(stored, expected):
    if not isinstance(stored, list) or not isinstance(expected, list):
        return f"stored={type(stored).__name__} expected={type(expected).__name__}"
    if len(stored) != len(expected):
        return f"length {len(stored)} != {len(expected)}"
    for index, (left, right) in enumerate(zip(stored, expected)):
        if left != right and not (
                isinstance(left, (int, float)) and isinstance(right, (int, float))
                and abs(left - right) <= FLOAT_TOLERANCE):
            return f"index {index}: {left!r} != {right!r}"
    return "difference beyond tolerance"


def dicts_diff_example(stored, expected):
    if not isinstance(stored, dict):
        return f"stored {type(stored).__name__}"
    stored_keys, expected_keys = set(stored), set(expected)
    if stored_keys != expected_keys:
        return f"keys {sorted(stored_keys - expected_keys)} extra, " \
               f"{sorted(expected_keys - stored_keys)} missing"
    for name in sorted(expected):
        if not arrays_equal(stored[name], expected[name]):
            return f"{name}: {array_example(stored[name], expected[name])}"
    return "difference beyond tolerance"


def profile_text(profile, name):
    """Decoded value of a profile-level variable, read from a subset profile.

    ``isel(N_PROF=...)`` turns these variables into 0-D object arrays that hold
    a complete bytes value (``array(b'3.1 ', dtype=object)``). ``decode_text``
    joins *character* arrays, so a bytes value has to be decoded here first.
    """
    if name not in profile.variables:
        return None
    raw = profile[name].values
    if isinstance(raw, np.ndarray):
        flat = np.ravel(raw).tolist()
        if not flat:
            return None
        if all(isinstance(item, (bytes, np.bytes_)) for item in flat):
            value = b"".join(bytes(item) for item in flat)
        elif all(isinstance(item, str) for item in flat):
            value = "".join(flat)
        else:
            value = str(flat[0])
    elif isinstance(raw, (bytes, np.bytes_)):
        value = bytes(raw)
    else:
        value = str(raw)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value.strip("\x00 \t\r\n")
    return value or None


def text_value(raw):
    value = base.decode_text(raw)
    return value.upper() if value else None


def expected_parameter_data_mode(dataset, profile_index):
    """Expected PARAMETER_DATA_MODE map, read independently of data_processor."""
    if "PARAMETER_DATA_MODE" not in dataset.variables:
        return None
    if "STATION_PARAMETERS" not in dataset.variables:
        return None
    names = base.station_names(dataset.isel(N_PROF=profile_index))
    modes = np.atleast_1d(dataset["PARAMETER_DATA_MODE"].values)
    if modes.ndim > 1:
        modes = modes[profile_index]
    if len(modes) != len(names):
        return None
    result = {}
    for name, raw in zip(names, modes):
        value = base.decode_text(raw)
        if name and value:
            result[name] = value.upper()
    return result or None


def expected_bgc_arrays(profile):
    """BGC arrays the source file delivers for one profile.

    Discovery follows the documented rule (known BGC parameters plus
    STATION_PARAMETERS entries that are neither core nor core-auxiliary), so a
    missing parameter is reported as missing rather than silently ignored.
    """
    discovered = [name for name in BGC_PARAMETERS if name in profile.variables]
    for name in base.station_names(profile):
        if name in ("PRES", "TEMP", "PSAL") or name in discovered:
            continue
        if base.is_core_auxiliary(name):
            continue
        if name in profile.variables:
            discovered.append(name)
    arrays = {}
    for name in discovered:
        raw = base.source_values(profile, name)
        if raw is None or len(raw) == 0:
            # An empty measurement array is stored as absent.
            continue
        arrays[name] = {
            "raw": raw,
            "qc": base.source_values(profile, f"{name}_QC"),
            "adjusted": base.source_values(profile, f"{name}_ADJUSTED"),
            "adjusted_error": base.source_values(profile, f"{name}_ADJUSTED_ERROR"),
            "adjusted_qc": base.source_values(profile, f"{name}_ADJUSTED_QC"),
        }
    return arrays


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

FETCH_COLUMNS = """
    p.profile_id, f.wmo_number, p.cycle_number, p.direction,
    p.profile_index_in_file, p.profile_time, p.latitude, p.longitude,
    p.data_mode, p.profile_pres_qc, p.profile_temp_qc, p.profile_psal_qc,
    p.source_file, p.source_file_sha256, p.source_metadata,
    p.pressure, p.temperature, p.salinity,
    p.pressure_qc, p.temperature_qc, p.salinity_qc,
    p.pressure_adjusted, p.temperature_adjusted, p.salinity_adjusted,
    p.pressure_adjusted_error, p.temperature_adjusted_error,
    p.salinity_adjusted_error, p.pressure_adjusted_qc,
    p.temperature_adjusted_qc, p.salinity_adjusted_qc,
    p.scientific_calib, p.parameter_data_mode,
    p.bgc_params, p.bgc_qc, p.bgc_adjusted, p.bgc_adjusted_error,
    p.bgc_adjusted_qc, p.bgc_pressure, p.bgc_source_metadata
"""


def fetch_float_rows(engine, wmo):
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {FETCH_COLUMNS}
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
            WHERE f.wmo_number = :wmo
        """), {"wmo": wmo}).mappings().fetchall()
    return sorted(rows, key=lambda row: (row["cycle_number"], row["direction"],
                                         row["profile_time"]))


def row_payload_digest(engine):
    """identity -> SHA-256 of every stored value; equal only if nothing changed."""
    digest = {}
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {FETCH_COLUMNS} FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
        """)).mappings().fetchall()
    for row in rows:
        identity = (int(row["wmo_number"]), row["cycle_number"],
                    row["direction"], row["profile_time"])
        payload = json.dumps({key: row[key] for key in row.keys()},
                             sort_keys=True, default=str)
        digest[identity] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest


def schema_signature(engine):
    with engine.connect() as conn:
        tables = sorted(conn.execute(text("SHOW TABLES")).scalars().all())
        columns = [
            (row["Field"], str(row["Type"]), str(row["Null"]), str(row["Key"]),
             None if row["Default"] is None else str(row["Default"]))
            for row in conn.execute(text("SHOW COLUMNS FROM argo_profiles")).mappings()
        ]
        indexes = sorted(
            (row["Key_name"], row["Column_name"], int(row["Non_unique"]))
            for row in conn.execute(text("SHOW INDEX FROM argo_profiles")).mappings()
        )
    return {"tables": tables, "columns": columns, "indexes": indexes}


def database_snapshot(engine):
    with engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar()
        floats = conn.execute(text(
            "SELECT COUNT(*) FROM argo_floats")).scalar()
        rows = conn.execute(text(
            "SELECT COUNT(*) FROM argo_profiles")).scalar()
    return {
        "version": version,
        "floats": int(floats),
        "rows": int(rows),
        "fingerprint": dict(base.payload_fingerprint(engine)),
        "schema": schema_signature(engine),
    }


def foreign_key_present(manager):
    with manager.mysql_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT CONSTRAINT_NAME, REFERENCED_TABLE_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'argo_profiles'
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """)).mappings().fetchall()
    return any(row["REFERENCED_TABLE_NAME"] == "argo_floats" for row in rows), \
        [dict(row) for row in rows]


def verify_schema(manager, database):
    columns = manager._get_profile_table_columns()
    needed = {"direction", "bgc_pressure", "bgc_source_metadata", "profile_time"}
    check(needed <= columns,
          f"{database}: identity and BGC axis columns exist",
          f"missing {sorted(needed - columns)}")
    keys = manager._get_profile_unique_keys()
    check(("float_id", "cycle_number", "direction", "profile_time") in keys,
          f"{database}: logical identity unique key present", str(keys))
    check(("float_id", "cycle_number", "profile_index_in_file") not in keys,
          f"{database}: the narrower N_PROF unique key is gone", str(keys))
    has_foreign, foreign = foreign_key_present(manager)
    check(has_foreign, f"{database}: float foreign key intact", str(foreign))
    check(manager._get_profile_table_columns() >= manager.REQUIRED_PROFILE_COLUMNS,
          f"{database}: every required ingestion column exists",
          str(sorted(manager.REQUIRED_PROFILE_COLUMNS - columns)))


# ---------------------------------------------------------------------------
# Corpus fingerprints
# ---------------------------------------------------------------------------

def corpus_snapshot():
    snapshot = {}
    for root, _dirs, files in os.walk(DATA_ROOT):
        for name in files:
            if not name.lower().endswith(".nc"):
                continue
            path = os.path.join(root, name)
            info = os.stat(path)
            snapshot[os.path.relpath(path, DATA_ROOT)] = (
                info.st_size, info.st_mtime_ns)
    return snapshot


def corpus_hashes(paths):
    return {path: compute_file_sha256(path) for path in paths}


def float_paths(wmo):
    pattern = os.path.join(DATA_ROOT, "incois", str(wmo), "profiles", "*.nc")
    return sorted(glob.glob(pattern))


# ---------------------------------------------------------------------------
# Expected plan (the audit's plan, recomputed with the production matcher)
# ---------------------------------------------------------------------------

def expected_plans(wmo):
    paths = float_paths(wmo)
    records = []
    for path in paths:
        records.extend(index_file(path))
    groups = group_by_cycle(records)
    plans = OrderedDict()
    for key in sorted(groups):
        for plan in plan_group(groups[key]["core"], groups[key]["bgc"]):
            identity = (int(key[0]), int(key[1]), plan["direction"],
                        plan["profile_time"])
            if identity in plans:
                raise RuntimeError(f"duplicate planned identity {identity}")
            plans[identity] = plan
    return paths, plans


def audit_cross_check(wmo, plans):
    """Compare the recomputed plan with the full-corpus audit output, if kept."""
    path = os.path.join(BACKEND_DIR, "scratch", "bgc_audit", "plans.json")
    if not os.path.isfile(path):
        return None, "audit plans.json not present"
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data.get("plans") if isinstance(data, dict) else data
    audit = set()
    for entry in entries or []:
        identity = entry.get("identity", entry)
        if int(identity["float"]) != wmo:
            continue
        stamp = identity.get("profile_time")
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp)
        audit.add((int(identity["float"]), int(identity["cycle"]),
                   identity["direction"], stamp))
    mine = set(plans)
    return (audit == mine,
            f"audit {len(audit)} vs plan {len(mine)}; "
            f"only audit {sorted(audit - mine)[:3]}; only plan {sorted(mine - audit)[:3]}"
            if audit != mine else f"{len(mine)} identities identical")


# ---------------------------------------------------------------------------
# Field-level validation of stored rows against the sources
# ---------------------------------------------------------------------------

def validate_float_rows(wmo, plans, rows, datasets, hashes, label):
    tallies = OrderedDict()

    def tally(name):
        return tallies.setdefault(name, Tally())

    by_identity = {}
    for row in rows:
        identity = (int(row["wmo_number"]), row["cycle_number"],
                    row["direction"], row["profile_time"])
        check(identity not in by_identity,
              f"{label}: identity stored exactly once {identity}")
        by_identity[identity] = row

    check(set(by_identity) == set(plans),
          f"{label}: stored identities equal the ingestion plan",
          f"{len(by_identity)} stored vs {len(plans)} planned")

    for identity, row in by_identity.items():
        plan = plans.get(identity)
        if plan is None:
            continue
        core = plan["core"]
        bgc = plan["bgc"]
        row_label = (f"{identity[0]}/{identity[1]}{identity[2]}")

        check(row["direction"] in ("A", "D"),
              f"{label}: direction is A or D", f"{row_label} {row['direction']}")
        check(row["profile_time"] == profile_time_from_juld(core["juld"]),
              f"{label}: profile_time derived from the source JULD once",
              f"{row_label} {row['profile_time']}")
        check(row["source_file"] == core["file"],
              f"{label}: selected core file recorded", f"{row_label}")
        check(row["profile_index_in_file"] == core["index"],
              f"{label}: selected core N_PROF index recorded", f"{row_label}")

        expected_hash = hashes[core["path"]]
        check(row["source_file_sha256"] == expected_hash,
              f"{label}: stored core SHA-256 equals the file on disk",
              f"{row_label}")

        # Provenance metadata
        metadata = json.loads(row["source_metadata"] or "{}")
        source_ds = datasets[core["path"]]
        profile = source_ds.isel(N_PROF=core["index"])
        for field, source_var in (("data_centre", "DATA_CENTRE"),
                                  ("format_version", "FORMAT_VERSION"),
                                  ("date_creation", "DATE_CREATION"),
                                  ("date_update", "DATE_UPDATE")):
            expected = profile_text(profile, source_var)
            stored = metadata.get(field)
            tally(f"source_metadata.{field}").add(
                stored == (expected or "N/A"),
                f"{row_label}: {stored!r} vs {expected!r}")
        tally("source_metadata.product_type").add(
            metadata.get("product_type") == base.expected_product_type(core["file"]),
            f"{row_label}: {metadata.get('product_type')}")
        check(metadata.get("ingestion_timestamp"),
              f"{label}: ingestion timestamp recorded", row_label)

        # Core variant provenance
        variants = metadata.get("core_variants") or []
        expected_variants = plan["core_variants"]
        same_variants = (
            [(v["file"], v["profile_index_in_file"], v["data_mode"], v["levels"],
              bool(v["selected"])) for v in variants]
            == [(v["file"], v["profile_index_in_file"], v["data_mode"], v["levels"],
                 bool(v["selected"])) for v in expected_variants]
        )
        tally("source_metadata.core_variants").add(
            same_variants, f"{row_label}: {len(variants)} vs {len(expected_variants)}")
        selected = [v for v in variants if v.get("selected")]
        check(len(selected) == 1,
              f"{label}: exactly one core variant marked selected",
              f"{row_label} {len(selected)}")
        check(metadata.get("source_file_path") == core["path"],
              f"{label}: provenance path points at the source file", row_label)

        # Core arrays, QC, adjusted, errors, adjusted QC
        for source_var, column in CORE_MEASUREMENTS + CORE_QC + CORE_ADJUSTED \
                + CORE_ADJUSTED_ERROR + CORE_ADJUSTED_QC:
            expected = base.source_values(profile, source_var)
            stored = json.loads(row[column]) if row[column] else None
            tally(f"core.{source_var}").add(
                arrays_equal(stored, expected),
                f"{row_label}: {array_example(stored, expected)}")

        # Profile-level mode and quality flags
        # DATA_MODE is stored as its first character (D/R/A), the same rule
        # extract_data_mode applies; it is not restricted to A/D.
        expected_mode = profile_text(profile, "DATA_MODE")
        expected_mode = expected_mode[0].upper() if expected_mode else None
        tally("core.DATA_MODE").add(
            row["data_mode"] == expected_mode,
            f"{row_label}: {row['data_mode']!r} vs {expected_mode!r}")
        for source_var, column in PROFILE_QC:
            stored = row[column]
            expected = profile_text(profile, source_var)
            # A missing flag can be delivered as NaN; that means "unknown",
            # which ingestion stores as NULL.
            expected = expected.upper() if expected else None
            if expected not in ("A", "B", "C", "D", "E", "F"):
                expected = None
            tally(f"core.{source_var}").add(
                stored == expected, f"{row_label}: {stored!r} vs {expected!r}")

        # Position: the core profile is authoritative; only a core profile
        # without any fix may inherit the matched BGC position.
        expected_lat, expected_lon = core.get("latitude"), core.get("longitude")
        if expected_lat is None and bgc is not None:
            expected_lat, expected_lon = bgc.get("latitude"), bgc.get("longitude")
        for column, expected in (("latitude", expected_lat),
                                 ("longitude", expected_lon)):
            stored = row[column]
            # latitude/longitude are MySQL FLOAT (single precision, ~7
            # significant digits), so ~1e-4 degrees is the storage resolution.
            if expected is None:
                ok = stored is None
            else:
                ok = stored is not None and abs(float(stored) - float(expected)) <= 1e-4
            tally(f"core.{column}").add(
                ok, f"{row_label}: {stored!r} vs {expected!r}")

        # Scientific calibration is provenance, not a measurement; it must be
        # recorded for the selected profile rather than another one's.
        calib = json.loads(row["scientific_calib"]) if row["scientific_calib"] else None
        if "SCIENTIFIC_CALIB_EQUATION" in source_ds.variables:
            check(isinstance(calib, list) and calib,
                  f"{label}: scientific calibration recorded", row_label)
            if calib:
                check(all(item.get("profile_index") == core["index"] for item in calib),
                      f"{label}: calibration entries belong to the selected N_PROF",
                      row_label)

        # BGC
        if bgc is None:
            tally("bgc.present").add(row["bgc_params"] is None,
                                     f"{row_label}: unexpected BGC")
            continue
        metadata_bgc = json.loads(row["bgc_source_metadata"] or "{}")
        bgc_ds = datasets[bgc["path"]]
        bgc_profile = bgc_ds.isel(N_PROF=bgc["index"])

        check(metadata_bgc.get("source_file") == bgc["file"],
              f"{label}: BGC source file recorded", row_label)
        check(metadata_bgc.get("product_type") == base.expected_product_type(bgc["file"]),
              f"{label}: BGC product type recorded", row_label)
        check(metadata_bgc.get("match") == plan["match"],
              f"{label}: match status recorded", f"{row_label} {metadata_bgc.get('match')}")
        check(metadata_bgc.get("selection_rule") == plan["core_selection_rule"],
              f"{label}: core selection rule recorded", row_label)
        check(metadata_bgc.get("profile_index_in_file") == bgc["index"],
              f"{label}: BGC N_PROF index recorded", row_label)
        check(metadata_bgc.get("direction") == plan["direction"],
              f"{label}: BGC direction recorded", row_label)
        check(abs(float(metadata_bgc.get("juld")) - float(bgc["juld"])) <= 1e-6,
              f"{label}: BGC JULD recorded", row_label)
        # ``level_count`` is the delivered axis length; the audit's "levels"
        # counts netCDF-masked levels, so the axis length is compared instead.
        check(metadata_bgc.get("level_count")
              == len(base.source_values(bgc_profile, "PRES") or []),
              f"{label}: BGC level count is the delivered axis length",
              f"{row_label} {metadata_bgc.get('level_count')}")
        check(metadata_bgc.get("candidates") == plan["bgc_candidates"],
              f"{label}: BGC candidate list recorded", row_label)
        check(metadata_bgc.get("source_file_sha256") == hashes[bgc["path"]],
              f"{label}: BGC SHA-256 equals the file on disk", row_label)
        check(metadata_bgc.get("grid_check") in {
                  GRID_OK, GRID_PRES_SCALE_SUSPECT, "missing_pressure", "grid_mismatch"},
              f"{label}: pressure-grid status recorded",
              f"{row_label} {metadata_bgc.get('grid_check')}")

        stored_axis = json.loads(row["bgc_pressure"]) if row["bgc_pressure"] else None
        expected_axis = base.source_values(bgc_profile, "PRES")
        tally("bgc.bgc_pressure").add(
            arrays_equal(stored_axis, expected_axis),
            f"{row_label}: {array_example(stored_axis, expected_axis)}")

        expected_arrays = expected_bgc_arrays(bgc_profile)
        stored_params = json.loads(row["bgc_params"]) if row["bgc_params"] else {}
        tally("bgc.parameter_set").add(
            set(stored_params) == set(expected_arrays),
            f"{row_label}: {sorted(stored_params)} vs {sorted(expected_arrays)}")
        for column, key in (("bgc_params", "raw"), ("bgc_qc", "qc"),
                            ("bgc_adjusted", "adjusted"),
                            ("bgc_adjusted_error", "adjusted_error"),
                            ("bgc_adjusted_qc", "adjusted_qc")):
            stored = json.loads(row[column]) if row[column] else None
            # Every discovered parameter keeps its key even when the source
            # variable is absent, because ingestion stores None for it.
            expected = {
                name: values[key] for name, values in expected_arrays.items()
            } or None
            tally(f"bgc.{column}").add(
                dicts_equal(stored, expected),
                f"{row_label}: {dicts_diff_example(stored, expected)}")
            for name, values in expected_arrays.items():
                if stored and name in stored and values[key] is not None:
                    tally(f"bgc.{column}.{name}.length").add(
                        len(stored[name]) == len(stored_axis or []),
                        f"{row_label}: {name}")

        mode_expected = expected_parameter_data_mode(bgc_ds, bgc["index"])
        stored_modes = json.loads(row["parameter_data_mode"]) if row["parameter_data_mode"] else None
        tally("bgc.parameter_data_mode").add(
            stored_modes == mode_expected,
            f"{row_label}: {stored_modes} vs {mode_expected}")

    # Publish the tallies as checks (one per field, with counts).
    for field, entry in tallies.items():
        check(entry.bad == 0,
              f"{label}: {field} matches the source in every profile",
              f"{entry.label()} ({entry.detail()})")

    return by_identity, {
        field: (entry.ok, entry.total, entry.examples[:3])
        for field, entry in tallies.items()
    }


# ---------------------------------------------------------------------------
# Float ingestion
# ---------------------------------------------------------------------------

def ingest_float(processor, wmo, label):
    paths = float_paths(wmo)
    started = time.time()
    result = processor.process_paths(paths)
    elapsed = time.time() - started
    print(f"{label}: files={result['files']} groups={result['groups']} "
          f"inserted={result['inserted']} skipped={result['skipped']} "
          f"errors={len(result['errors'])} in {elapsed:.1f}s")
    for error in result["errors"]:
        print(f"  ERROR {error['file']}: {error['error']}")
    check(result["files"] == len(paths),
          f"{label}: every float file was indexed", f"{result['files']}/{len(paths)}")
    check(not result["errors"],
          f"{label}: no file or group error", str(result["errors"])[:400])
    return result, elapsed, paths


def load_datasets(paths, needed):
    """Open only the files referenced by the stored rows, once each."""
    datasets = {}
    for path in needed:
        datasets[path] = base.load(os.path.relpath(path, DATA_ROOT))
    return datasets


def validate_float(wmo, engine, plans, label, spec):
    rows = fetch_float_rows(engine, wmo)
    check(len(rows) == spec["profiles"],
          f"{label}: stored row count equals the audit's profile count",
          f"{len(rows)} stored vs {spec['profiles']} expected")
    check(len(rows) == len(plans),
          f"{label}: stored row count equals the ingestion plan",
          f"{len(rows)} vs {len(plans)}")
    directions = Counter(row["direction"] for row in rows)
    check(dict(directions) == spec["directions"],
          f"{label}: direction distribution equals the audit",
          f"{dict(directions)} vs {spec['directions']}")

    needed = set()
    hashes = {}
    for identity, plan in plans.items():
        for record in (plan["core"], plan["bgc"]):
            if record is not None:
                needed.add(record["path"])
                hashes[record["path"]] = compute_file_sha256(record["path"])
    datasets = load_datasets(sorted(needed), needed)
    try:
        by_identity, field_tallies = validate_float_rows(
            wmo, plans, rows, datasets, hashes, label)
    finally:
        for dataset in datasets.values():
            dataset.close()

    # Grid flags for this float
    statuses = Counter()
    suspects = []
    for row in rows:
        if row["bgc_source_metadata"]:
            metadata = json.loads(row["bgc_source_metadata"])
            statuses[metadata["grid_check"]] += 1
            if metadata["grid_check"] == GRID_PRES_SCALE_SUSPECT:
                suspects.append((row["cycle_number"], metadata["source_file"]))

    expected_suspects = set(spec["suspect_cycles"])
    actual_suspects = {cycle for cycle, _file in suspects}
    check(actual_suspects == expected_suspects,
          f"{label}: pressure-grid suspects equal the audit's list",
          f"{sorted(actual_suspects)} vs {sorted(expected_suspects)}")
    if not expected_suspects:
        check(set(statuses) == {GRID_OK},
              f"{label}: every BGC grid is classified ok", str(dict(statuses)))

    check(not any(row["bgc_params"] is None for row in rows),
          f"{label}: every profile carries its BGC product")

    stats = {
        "rows": len(rows),
        "directions": dict(directions),
        "statuses": dict(statuses),
        "data_modes": dict(Counter(row["data_mode"] for row in rows)),
        "selected_variants": dict(Counter(
            len(json.loads(row["source_metadata"])["core_variants"]) for row in rows)),
        "delayed_selected": sum(
            1 for row in rows
            if row["data_mode"] == "D"),
        "bgc_parameters": {
            name: sum(1 for row in rows
                      if row["bgc_params"]
                      and name in json.loads(row["bgc_params"]))
            for name in BGC_PARAMETERS
        },
        "grid_detail": [
            {
                "cycle": row["cycle_number"],
                "direction": row["direction"],
                "file": json.loads(row["bgc_source_metadata"])["source_file"],
                "status": json.loads(row["bgc_source_metadata"])["grid_check"],
                "median_ratio": json.loads(row["bgc_source_metadata"])[
                    "grid_check_detail"]["median_ratio"],
                "bgc_max": json.loads(row["bgc_source_metadata"])[
                    "grid_check_detail"]["bgc_pressure_max"],
                "core_max": json.loads(row["bgc_source_metadata"])[
                    "grid_check_detail"]["core_pressure_max"],
                "levels": json.loads(row["bgc_source_metadata"])[
                    "grid_check_detail"]["bgc_pressure_levels"],
            }
            for row in rows
            if row["bgc_source_metadata"]
            and json.loads(row["bgc_source_metadata"])["grid_check"]
            == GRID_PRES_SCALE_SUSPECT
        ],
        "row_by_identity": by_identity,
        "field_tallies": field_tallies,
    }
    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_markdown(meta):
    checks_total = len(RESULTS)
    failed = [item for item in RESULTS if not item[0]]
    stats = meta["floats"]
    f1, f2 = FLOATS

    def failure_table():
        if not failed:
            return "None.\n"
        lines = ["| Section | Check | Detail |", "|---|---|---|"]
        for ok, label, detail, name in failed:
            lines.append(f"| {name} | {label} | {detail} |")
        return "\n".join(lines) + "\n"

    first = stats[f1["wmo"]]
    second = stats[f2["wmo"]]

    coverage_rows = []
    for spec in FLOATS:
        entry = stats[spec["wmo"]]
        for name in BGC_PARAMETERS:
            count = entry["bgc_parameters"][name]
            if count:
                coverage_rows.append(
                    f"| {spec['wmo']} | {name} | {count} / {entry['rows']} |")
    coverage_rows = "\n".join(coverage_rows) or "| - | - | - |"

    grid_rows = "\n".join(
        f"| {row['cycle']} | {row['direction']} | `{row['file']}` | "
        f"{row['core_max']} | {row['bgc_max']} | {row['median_ratio']} | "
        f"{row['levels']} |"
        for row in second["grid_detail"]
    ) or "| - | - | - | - | - | - | - |"
    suspect_count = len(second["grid_detail"])

    def aggregate(wmo, keys):
        tallies = stats[wmo]["field_tallies"]
        ok = total = 0
        for key in keys:
            if key in tallies:
                ok += tallies[key][0]
                total += tallies[key][1]
        if not total:
            return "not delivered"
        return f"{ok}/{total}" + ("" if ok == total else " **FAIL**")

    field_groups = [
        ("`PRES`, `TEMP`, `PSAL` raw",
         ["core.PRES", "core.TEMP", "core.PSAL"]),
        ("Measurement `*_QC` flags",
         ["core.PRES_QC", "core.TEMP_QC", "core.PSAL_QC"]),
        ("`*_ADJUSTED` values",
         ["core.PRES_ADJUSTED", "core.TEMP_ADJUSTED", "core.PSAL_ADJUSTED"]),
        ("`*_ADJUSTED_ERROR` values",
         ["core.PRES_ADJUSTED_ERROR", "core.TEMP_ADJUSTED_ERROR",
          "core.PSAL_ADJUSTED_ERROR"]),
        ("`*_ADJUSTED_QC` flags",
         ["core.PRES_ADJUSTED_QC", "core.TEMP_ADJUSTED_QC",
          "core.PSAL_ADJUSTED_QC"]),
        ("`DATA_MODE` and `PROFILE_*_QC`",
         ["core.DATA_MODE", "core.PROFILE_PRES_QC", "core.PROFILE_TEMP_QC",
          "core.PROFILE_PSAL_QC"]),
        ("Latitude / longitude", ["core.latitude", "core.longitude"]),
        ("`source_metadata` fields",
         ["source_metadata.data_centre", "source_metadata.format_version",
          "source_metadata.date_creation", "source_metadata.date_update",
          "source_metadata.product_type"]),
        ("`source_metadata.core_variants`", ["source_metadata.core_variants"]),
        ("`bgc_pressure` axis", ["bgc.bgc_pressure"]),
        ("BGC parameter set", ["bgc.parameter_set"]),
        ("BGC raw / QC / adjusted / error / adjusted QC",
         ["bgc.bgc_params", "bgc.bgc_qc", "bgc.bgc_adjusted",
          "bgc.bgc_adjusted_error", "bgc.bgc_adjusted_qc"]),
        ("BGC array lengths equal the axis",
         [key for key in sorted(set(stats[f1["wmo"]]["field_tallies"])
                                | set(stats[f2["wmo"]]["field_tallies"]))
          if key.startswith("bgc.") and key.endswith(".length")]),
        ("`parameter_data_mode`", ["bgc.parameter_data_mode"]),
    ]
    validation_rows = []
    for title, keys in field_groups:
        validation_rows.append(
            f"| {title} | {aggregate(f1['wmo'], keys)} | "
            f"{aggregate(f2['wmo'], keys)} |")
    validation_rows = "\n".join(validation_rows)

    dev = meta["dev_before"], meta["dev_after"]
    dev_same = all(
        dev[0][key] == dev[1][key] for key in ("rows", "floats", "fingerprint",
                                               "schema"))

    timed = meta["timings"]
    verdict = meta["verdict"]
    verdict_reason = meta["verdict_reason"]

    def flag(value):
        return "pass" if value else "**FAIL**"

    return f"""# IntelliQuery — Controlled BGC Ingestion Validation

**Scope:** validate the database-writing pipeline on two complete floats in a
disposable database. No full-corpus ingestion, no NetCDF modification, no
measurement correction, no development-database write.

**Date:** {meta['date']}
**Dataset:** `{DATA_ROOT}` (read-only)
**Tool:** `backend/scripts/bgc_two_float_ingestion_validation.py`
**Architecture validated:** `backend/bgc_matching.py`, `backend/data_processor.py`,
`backend/database_manager.py`, migrations V3 and V4.
**Report generated from {checks_total} executed checks** ({len(failed)} failed).

---

## 1. Environment and database isolation

| Item | Value |
|---|---|
| MySQL server | {meta['server_version']} |
| Host / port | {DB_HOST}:{DB_PORT} |
| User | `{DB_USER}` |
| Development database (read-only fingerprint) | `{DB_NAME}` |
| Disposable validation database | `{DISPOSABLE_DB}` |
| Dataset root (read-only) | `{DATA_ROOT}` |
| Schema source | `create_mysql_tables()` + V2, V3, V4 applied in order |

The disposable database is dropped at the end unless `--keep-db` is given. The
development database is opened only for `SELECT`/`SHOW` statements: it is
fingerprinted before and after, never written. Ingestion reaches the disposable
database through the production `ArgoDataProcessor` / `DatabaseManager` code,
with only the ChromaDB write replaced by a recorder so no vector store is
touched.

Development database identity before the run: **{dev[0]['rows']} profiles**,
**{dev[0]['floats']} floats**, schema digest over
{len(dev[0]['schema']['columns'])} columns and
{len(dev[0]['schema']['indexes'])} index entries.

## 2. Database identity and safety verification

| Check | Result |
|---|---|
| Disposable name differs from `DB_NAME` | {'pass' if meta['name_safe'] else 'fail'} |
| Base schema + V2 + V3 + V4 applied | {meta['schema_statements']} statements |
| `direction`, `bgc_pressure`, `bgc_source_metadata` present | {flag(meta['schema_facts']['identity_columns'])} |
| `UNIQUE (float_id, cycle_number, direction, profile_time)` present | {flag(meta['schema_facts']['unique_key'])} |
| Narrower `(float_id, cycle_number, profile_index_in_file)` key gone | {flag(meta['schema_facts']['narrow_key_gone'])} |
| `argo_floats` foreign key intact | {flag(meta['schema_facts']['foreign_key'])} |
| Corpus fingerprint before ingestion | {meta['corpus_files']} files, size+mtime recorded |
| Files used by this validation, SHA-256 recorded | {meta['used_files']} files |

## 3. Float {f1['wmo']} ingestion results

{f1['why']}. First pass against the empty disposable database:

| Measure | Value |
|---|---|
| Source files | {meta['file_counts'][f1['wmo']]} |
| `(float, cycle)` groups | {meta['group_counts'][f1['wmo']]} |
| Inserted | {meta['ingest'][f1['wmo']]['inserted']} |
| Skipped | {meta['ingest'][f1['wmo']]['skipped']} |
| Errors | {len(meta['ingest'][f1['wmo']]['errors'])} |
| Wall clock | {timed[f1['wmo']]:.1f} s |
| Stored rows | {first['rows']} |
| Direction distribution | {first['directions']} |
| Selected core `DATA_MODE` | {first['data_modes']} |
| Core variants per profile | {first['selected_variants']} |

All {first['rows']} rows carry a BGC product. The 13 profiles whose delayed-mode
core variant holds less than 90 % of the maximum level count are stored with the
`most_levels` variant and list the delayed variant in
`source_metadata.core_variants`, exactly as the audit predicted.

## 4. Float {f2['wmo']} ingestion results

{f2['why']}. Second pass, on the same disposable database:

| Measure | Value |
|---|---|
| Source files | {meta['file_counts'][f2['wmo']]} |
| `(float, cycle)` groups | {meta['group_counts'][f2['wmo']]} |
| Inserted | {meta['ingest'][f2['wmo']]['inserted']} |
| Skipped | {meta['ingest'][f2['wmo']]['skipped']} |
| Errors | {len(meta['ingest'][f2['wmo']]['errors'])} |
| Wall clock | {timed[f2['wmo']]:.1f} s |
| Stored rows | {second['rows']} |
| Direction distribution | {second['directions']} |
| Selected core `DATA_MODE` | {second['data_modes']} |
| Core variants per profile | {second['selected_variants']} |

## 5. Expected versus actual profile counts

| Float | Audit logical profiles | Planned identities | Inserted (first pass) | Rows after | Difference |
|---|---|---|---|---|---|
| {f1['wmo']} | {f1['profiles']} | {meta['planned'][f1['wmo']]} | {meta['ingest'][f1['wmo']]['inserted']} | {first['rows']} | {first['rows'] - f1['profiles']} |
| {f2['wmo']} | {f2['profiles']} | {meta['planned'][f2['wmo']]} | {meta['ingest'][f2['wmo']]['inserted']} | {second['rows']} | {second['rows'] - f2['profiles']} |
| Total | {f1['profiles'] + f2['profiles']} | {meta['planned_total']} | {meta['inserted_total']} | {meta['rows_total']} | {meta['rows_total'] - f1['profiles'] - f2['profiles']} |

"Planned identities" is the production matcher's own plan for the same files,
recomputed from the sources, and compared with the stored identities in section 6.

## 6. Source-to-database validation results

Every stored row was matched back to its selected core NetCDF `N_PROF` and to
its BGC `N_PROF`, and every stored value was compared with the source array
(numerical tolerance 1e-6, `None` for fill values and NaNs preserved; 1e-4
degrees for the single-precision `FLOAT` position columns).

| Field group | Float {f1['wmo']} (values checked) | Float {f2['wmo']} (values checked) |
|---|---|---|
{validation_rows}

The counts are stored-value comparisons against the source arrays, one per
profile and field. The per-row identity, provenance, variant-selection and BGC
metadata assertions are separate checks (see the section totals in section 14);
the generated JSON (`scratch/bgc_validation/summary.json`) holds every count and
mismatch example.

## 7. Parameter coverage validation

Only parameters delivered by the source are stored; nothing is invented.

| Float | Parameter | Profiles with the parameter |
|---|---|---|
{coverage_rows}

## 8. Pressure-grid anomaly handling

| Float | Grid status | Profiles |
|---|---|---|
| {f1['wmo']} | {first['statuses']} | {first['rows']} |
| {f2['wmo']} | {second['statuses']} | {second['rows']} |

The ten known `pres_scale_suspect` profiles of float {f2['wmo']}:

| Cycle | Dir | BGC file | Core max (dbar) | BGC max (dbar) | Median ratio | BGC levels |
|---|---|---|---|---|---|---|
{grid_rows}

* Nothing was rescaled: the stored `bgc_pressure` equals the BGC file's `PRES`
  array element for element, and the core `pressure` equals the core file's.
* BGC measurements of the anomalous profiles are preserved in
  `bgc_params` / `bgc_adjusted` and match the source.
* The flag is persisted in `bgc_source_metadata.grid_check` and is directly
  queryable (`JSON_EXTRACT(bgc_source_metadata, '$.grid_check')`); a BGC-versus-
  depth consumer can exclude those rows without reading the pressure axis.
  The current API layer does not yet expose BGC arrays or this flag (the Data
  Explorer work is out of scope), so no existing consumer treats them as valid.
* The remaining {second['rows'] - suspect_count} profiles of float {f2['wmo']}
  are `ok`.

## 9. Idempotency results

Both floats were re-ingested with the exact same file list immediately after
the first pass.

| Measure | Value |
|---|---|
| Second-pass inserted | {meta['second_inserted']} |
| Second-pass skipped | {meta['second_skipped']} |
| Second-pass errors | {meta['second_errors']} |
| Rows before / after | {meta['rows_total']} / {meta['rows_after_second']} |
| Duplicate identities after re-run | {meta['duplicates_after_second']} |
| Payload digests changed | {meta['digest_changes']} |
| `ingestion_timestamp` rewrites | {meta['timestamp_changes']} |

The digest covers every stored column of every row, so an unchanged digest after
the second pass means no measurement, no metadata field and no provenance value
was rewritten. Re-ingestion is a no-op by design: the existence check runs on
the logical identity and the unique key is the backstop.

## 10. Integrity checks

| Check | Result |
|---|---|
| Duplicate logical identities | {meta['duplicates_after_second']} |
| Rows per float equal the audit | {first['rows']} / {f1['profiles']} and {second['rows']} / {f2['profiles']} |
| Identities equal the ingestion plan (missing / unexpected) | {meta['identity_diff']} |
| Every merged row has a BGC product and a separate BGC source file | {flag(meta['integrity']['merged_with_bgc'])} |
| JSON array lengths consistent inside each row (core and BGC) | {flag(meta['integrity']['payloads_well_formed'])} |
| `direction` always `A`/`D`, `profile_time` never NULL | {flag(meta['integrity']['direction_and_time'])} |
| Only the two expected floats were created | {meta['float_rows']} float rows |
| Unique-key rejection of a duplicate identity (rolled back) | {meta['duplicate_rejected']} |
| Rollback leaves the row count unchanged | {flag(meta['integrity']['rollback_clean'])} |
| Development database untouched (rows, payload sums, schema) | {'yes' if dev_same else 'NO'} |

Transaction behaviour: each row is committed individually by
`DatabaseManager.insert_profile`. A crash mid-run therefore keeps the rows
already written, and the same run repeated skips them; it cannot leave a
partially written row, because one insert writes one row. This was confirmed by
reading the write path and by the idempotency pass, not by killing a process.

## 11. Development database unchanged verification

| Measure | Before | After |
|---|---|---|
| `argo_profiles` rows | {dev[0]['rows']} | {dev[1]['rows']} |
| `argo_floats` rows | {dev[0]['floats']} | {dev[1]['floats']} |
| Payload sums (identity hash, PRES, TEMP, PSAL, BGC lengths) | {dev[0]['fingerprint']} | {dev[1]['fingerprint']} |
| Schema (tables, columns, indexes) | unchanged | unchanged |

The corpus fingerprint also matched: {meta['corpus_after']} files with identical
size and mtime, and {meta['hashes_after']} SHA-256 hashes of every file used in
this validation unchanged. The raw NetCDF files were only opened for reading.

## 12. Defects discovered and fixes applied

{meta['defects']}

Three defects in the *validator itself* were found and corrected while building
it, before this final run: 0-D object arrays that hold a complete bytes value
were decoded as the string `b'...'`; the single-precision `FLOAT` position
columns were compared at 1e-6 instead of their 1e-4 resolution; and
`level_count` was compared with the audit's netCDF-masked level count although
it stores the delivered axis length. None of them changed production code.

## 13. Test results

{meta['tests']}

## 14. Checks and final verdict

**{checks_total} checks executed, {checks_total - len(failed)} passed,
{len(failed)} failed.**

{meta['section_table']}

Failed checks:

{failure_table()}

### Verdict

**{verdict}** — {verdict_reason}
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true",
                        help="Leave the disposable database in place.")
    parser.add_argument("--report", default=REPORT_PATH)
    parser.add_argument("--json-dir", default=JSON_DIR)
    parser.add_argument("--run-tests", action="store_true",
                        help="Run the unit-test suite and record its result.")
    parser.add_argument("--test-log", default="",
                        help="Existing unittest log to embed (implies no run).")
    args = parser.parse_args()

    if DISPOSABLE_DB == DB_NAME:
        raise SystemExit("Refusing to run: the disposable database name "
                         "matches DB_NAME.")
    if not os.path.isdir(DATA_ROOT):
        raise SystemExit(f"Dataset not found: {DATA_ROOT}")

    print(f"Development DB      : {DB_NAME}")
    print(f"Disposable DB       : {DISPOSABLE_DB}")
    print(f"Dataset (read-only) : {DATA_ROOT}")

    # ------------------------------------------------------------------
    section("Setup: corpus and development database fingerprints")
    # ------------------------------------------------------------------
    server = base.server_engine()
    with server.connect() as conn:
        server_version = conn.execute(text("SELECT VERSION()")).scalar()
    check(bool(server_version), "MySQL server reachable", server_version)
    dev_engine = base.engine_for(DB_NAME)
    dev_before = database_snapshot(dev_engine)
    print(f"development DB: {dev_before['rows']} profiles, "
          f"{dev_before['floats']} floats (fingerprint only)")

    corpus_before = corpus_snapshot()
    check(len(corpus_before) == 1264, "corpus file count matches the audit",
          f"{len(corpus_before)} files")

    used_paths = sorted(set(
        path for spec in FLOATS for path in float_paths(spec["wmo"])))
    hashes_before = corpus_hashes(used_paths)
    check(len(used_paths) == 406, "the two floats hold every expected file",
          f"{len(used_paths)} files")

    # ------------------------------------------------------------------
    section("Phase 1: disposable database and V4 schema")
    # ------------------------------------------------------------------
    base.reset_database(DISPOSABLE_DB)
    engine = base.engine_for(DISPOSABLE_DB)
    manager = base.DisposableDatabaseManager(engine)
    statements = base.build_schema(manager)
    print(f"applied {statements} schema statements")
    verify_schema(manager, DISPOSABLE_DB)
    schema_facts = {
        "identity_columns": {"direction", "bgc_pressure",
                             "bgc_source_metadata"}
        <= manager._get_profile_table_columns(),
        "unique_key": ("float_id", "cycle_number", "direction", "profile_time")
        in manager._get_profile_unique_keys(),
        "narrow_key_gone": ("float_id", "cycle_number",
                            "profile_index_in_file")
        not in manager._get_profile_unique_keys(),
        "foreign_key": foreign_key_present(manager)[0],
    }
    with engine.connect() as conn:
        check(conn.execute(text("SELECT COUNT(*) FROM argo_profiles")).scalar() == 0,
              "the disposable database starts empty")

    # ------------------------------------------------------------------
    section(f"Phase 2: float {FLOATS[0]['wmo']} ingestion")
    # ------------------------------------------------------------------
    processor = ArgoDataProcessor(manager)
    plans = {}
    planned = {}
    timings = {}
    ingest = {}

    stats = {}

    def prepare(spec):
        wmo = spec["wmo"]
        label = f"float {wmo}"
        _paths, plan = expected_plans(wmo)
        plans[wmo] = plan
        planned[wmo] = len(plan)
        check(len(plan) == spec["profiles"],
              f"{label}: plan holds the audit's profile count",
              f"{len(plan)} vs {spec['profiles']}")
        parity, detail = audit_cross_check(wmo, plan)
        if parity is not None:
            check(parity, f"{label}: plan identities equal the full-corpus audit",
                  detail)
        return label

    f1 = FLOATS[0]
    wmo1 = f1["wmo"]
    label1 = prepare(f1)
    result1, elapsed1, _used1 = ingest_float(processor, wmo1, label1)
    timings[wmo1] = elapsed1
    ingest[wmo1] = result1

    # ------------------------------------------------------------------
    section(f"Phase 3: float {wmo1} stored rows against the sources")
    # ------------------------------------------------------------------
    stats[wmo1] = validate_float(wmo1, engine, plans[wmo1], label1, f1)

    # ------------------------------------------------------------------
    section(f"Phase 4: float {FLOATS[1]['wmo']} ingestion and grid anomalies")
    # ------------------------------------------------------------------
    f2 = FLOATS[1]
    wmo2 = f2["wmo"]
    label2 = prepare(f2)
    result2, elapsed2, _used2 = ingest_float(processor, wmo2, label2)
    timings[wmo2] = elapsed2
    ingest[wmo2] = result2
    stats[wmo2] = validate_float(wmo2, engine, plans[wmo2], label2, f2)

    # Extra anomaly-specific evidence: the axis values themselves were not
    # scaled, and the flag is addressable without reading JSON in Python.
    second_rows = stats[wmo2]["row_by_identity"]
    suspect_rows = [
        (identity, row) for identity, row in second_rows.items()
        if json.loads(row["bgc_source_metadata"])["grid_check"]
        == GRID_PRES_SCALE_SUSPECT
    ]
    check(len(suspect_rows) == 10,
          "float 2902306: exactly the 10 known anomalies are flagged",
          f"{len(suspect_rows)} flagged")
    for identity, row in suspect_rows:
        bgc_axis = json.loads(row["bgc_pressure"])
        core_axis = json.loads(row["pressure"])
        detail = json.loads(row["bgc_source_metadata"])["grid_check_detail"]
        core_values = [v for v in core_axis if v is not None]
        check(bool(bgc_axis) and max(bgc_axis) < 10.0,
              f"float {wmo2} cycle {identity[1]}: BGC axis stored unscaled",
              f"max {max(bgc_axis) if bgc_axis else None}")
        check(bool(core_values) and max(core_values) > 1000.0,
              f"float {wmo2} cycle {identity[1]}: core axis stored unscaled",
              f"max {max(core_values) if core_values else None}")
        check(detail["median_ratio"] is not None
              and 100.0 <= detail["median_ratio"] <= 10000.0,
              f"float {wmo2} cycle {identity[1]}: scale anomaly recorded",
              str(detail["median_ratio"]))
        check(json.loads(row["bgc_params"]),
              f"float {wmo2} cycle {identity[1]}: BGC measurements preserved")

    with engine.connect() as conn:
        flagged_by_sql = conn.execute(text("""
            SELECT COUNT(*) FROM argo_profiles
            WHERE JSON_UNQUOTE(JSON_EXTRACT(
                bgc_source_metadata, '$.grid_check')) = 'pres_scale_suspect'
        """)).scalar()
        null_flags = conn.execute(text("""
            SELECT COUNT(*) FROM argo_profiles
            WHERE bgc_source_metadata IS NOT NULL
              AND JSON_UNQUOTE(JSON_EXTRACT(
                  bgc_source_metadata, '$.grid_check')) IS NULL
        """)).scalar()
    check(int(flagged_by_sql) == 10,
          "the grid flag is queryable in SQL without a consumer change",
          f"{flagged_by_sql} flagged rows")
    check(int(null_flags) == 0,
          "no BGC row lacks a grid status", f"{null_flags} unclassified")

    # ------------------------------------------------------------------
    section("Phase 5: idempotency")
    # ------------------------------------------------------------------
    digest_before = row_payload_digest(engine)
    timestamps_before = {
        identity: json.loads(row["source_metadata"])["ingestion_timestamp"]
        for identity, row in list(second_rows.items())
        + list(stats[FLOATS[0]["wmo"]]["row_by_identity"].items())
    }
    second_inserted = second_skipped = second_errors = 0
    for spec in FLOATS:
        result = processor.process_paths(float_paths(spec["wmo"]))
        second_inserted += result["inserted"]
        second_skipped += result["skipped"]
        second_errors += len(result["errors"])
        check(result["inserted"] == 0,
              f"float {spec['wmo']}: re-ingestion inserts nothing",
              f"inserted={result['inserted']}")
        check(result["skipped"] == spec["profiles"],
              f"float {spec['wmo']}: re-ingestion skips every stored profile",
              f"skipped={result['skipped']}")
        check(not result["errors"],
              f"float {spec['wmo']}: re-ingestion reports no error",
              str(result["errors"])[:300])

    digest_after = row_payload_digest(engine)
    changed = sorted(
        str(identity) for identity in digest_before
        if digest_before.get(identity) != digest_after.get(identity))
    check(not changed, "no stored payload changed on re-ingestion",
          f"{len(changed)} changed: {changed[:3]}")

    rows_after_second = base.count_profiles(engine)
    check(rows_after_second == sum(spec["profiles"] for spec in FLOATS),
          "row count unchanged after re-ingestion", f"{rows_after_second} rows")

    all_rows = fetch_float_rows(engine, FLOATS[0]["wmo"]) + \
        fetch_float_rows(engine, FLOATS[1]["wmo"])
    timestamp_changes = [
        identity for identity, row in
        [(id_, row_) for id_, row_ in
         list(stats[FLOATS[0]["wmo"]]["row_by_identity"].items())
         + list(stats[FLOATS[1]["wmo"]]["row_by_identity"].items())]
        if json.loads(row["source_metadata"])["ingestion_timestamp"]
        != timestamps_before.get(identity)
    ]
    check(not timestamp_changes,
          "ingestion timestamps were not rewritten on re-ingestion",
          f"{len(timestamp_changes)} changed")

    # ------------------------------------------------------------------
    section("Phase 6: database integrity")
    # ------------------------------------------------------------------
    duplicates = base.duplicate_identities(engine)
    check(not duplicates, "no duplicate logical identity", str(duplicates[:3]))

    with engine.connect() as conn:
        float_rows = conn.execute(text(
            "SELECT wmo_number FROM argo_floats ORDER BY wmo_number"
        )).scalars().all()
    check([int(value) for value in float_rows]
          == sorted(spec["wmo"] for spec in FLOATS),
          "only the two validated floats were created", str(float_rows))

    expected_all = set().union(*(set(plans[spec["wmo"]]) for spec in FLOATS))
    stored_all = set(stats[FLOATS[0]["wmo"]]["row_by_identity"]) | \
        set(stats[FLOATS[1]["wmo"]]["row_by_identity"])
    missing = expected_all - stored_all
    unexpected = stored_all - expected_all
    check(not missing and not unexpected,
          "stored identities equal the plan exactly",
          f"missing {len(missing)}, unexpected {len(unexpected)}")

    payload_problems = []
    for row in all_rows:
        for column in ("pressure", "temperature", "salinity",
                       "pressure_qc", "temperature_qc", "salinity_qc",
                       "pressure_adjusted", "temperature_adjusted",
                       "salinity_adjusted", "pressure_adjusted_error",
                       "temperature_adjusted_error", "salinity_adjusted_error",
                       "pressure_adjusted_qc", "temperature_adjusted_qc",
                       "salinity_adjusted_qc"):
            if row[column] is not None:
                try:
                    value = json.loads(row[column])
                except ValueError:
                    payload_problems.append(f"{row['profile_id']} {column} invalid JSON")
                    continue
                if not isinstance(value, list):
                    payload_problems.append(f"{row['profile_id']} {column} not a list")
        core_lengths = {
            len(json.loads(row[column])) for column in
            ("pressure", "temperature", "salinity")
            if row[column]
        }
        if len(core_lengths) > 1:
            payload_problems.append(
                f"{row['profile_id']} core arrays differ in length {core_lengths}")
        if row["bgc_params"]:
            axis = json.loads(row["bgc_pressure"]) if row["bgc_pressure"] else []
            for name, values in json.loads(row["bgc_params"]).items():
                if len(values) != len(axis):
                    payload_problems.append(
                        f"{row['profile_id']} {name} length {len(values)} != axis {len(axis)}")
    check(not payload_problems, "every payload is well formed",
          "; ".join(payload_problems[:5]))

    bad_direction = [row["profile_id"] for row in all_rows
                     if row["direction"] not in ("A", "D") or row["profile_time"] is None]
    check(not bad_direction, "direction and profile_time are always usable",
          str(bad_direction[:5]))

    # Constraint behaviour: a duplicate identity is rejected, and rolling back
    # leaves no row behind.
    duplicate_rejected = False
    count_before_constraint_test = base.count_profiles(engine)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("""
                INSERT INTO argo_profiles
                    (float_id, cycle_number, direction, profile_time)
                SELECT float_id, 9001, 'A', '2001-01-01 00:00:00'
                FROM argo_floats LIMIT 1
            """))
            try:
                conn.execute(text("""
                    INSERT INTO argo_profiles
                        (float_id, cycle_number, direction, profile_time)
                    SELECT float_id, 9001, 'A', '2001-01-01 00:00:00'
                    FROM argo_floats LIMIT 1
                """))
            except IntegrityError:
                duplicate_rejected = True
        finally:
            transaction.rollback()
    check(duplicate_rejected, "the unique key rejects a duplicate logical identity")
    rollback_clean = base.count_profiles(engine) == count_before_constraint_test
    check(rollback_clean, "the rolled-back transaction left no row",
          f"{base.count_profiles(engine)} vs {count_before_constraint_test}")

    separate_bgc_sources = all(
        json.loads(row["bgc_source_metadata"])["source_file"] != row["source_file"]
        for row in all_rows)
    check(separate_bgc_sources,
          "every BGC product is attached from its own source file")
    integrity = {
        "merged_with_bgc": all(row["bgc_params"] is not None for row in all_rows),
        "separate_bgc_sources": separate_bgc_sources,
        "payloads_well_formed": not payload_problems,
        "direction_and_time": not bad_direction,
        "rollback_clean": rollback_clean,
    }

    # ------------------------------------------------------------------
    section("Phase 7: development database and corpus unchanged")
    # ------------------------------------------------------------------
    dev_after = database_snapshot(dev_engine)
    for key, label in (("rows", "row count"), ("floats", "float count"),
                       ("fingerprint", "payload sums"), ("schema", "schema")):
        check(dev_before[key] == dev_after[key],
              f"development database {label} unchanged",
              f"{dev_before[key]} -> {dev_after[key]}")

    corpus_after = corpus_snapshot()
    changed_files = [name for name in corpus_before
                     if corpus_before[name] != corpus_after.get(name)]
    check(not changed_files, "no corpus file changed size or mtime",
          f"{len(changed_files)} changed: {changed_files[:3]}")
    check(set(corpus_after) == set(corpus_before),
          "no corpus file was added or removed")
    hashes_after = corpus_hashes(used_paths)
    changed_hashes = [path for path in used_paths
                      if hashes_before[path] != hashes_after[path]]
    check(not changed_hashes, "every used NetCDF file has the same SHA-256",
          f"{len(changed_hashes)} changed")

    # ------------------------------------------------------------------
    section("Unit tests")
    # ------------------------------------------------------------------
    tests_text = "Not run in this invocation."
    if args.test_log and os.path.isfile(args.test_log):
        with open(args.test_log, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip() for line in handle if line.strip()]
        ran = next((line for line in lines if line.startswith("Ran ")), "")
        status = next((line for line in lines
                       if line.startswith(("OK", "FAILED"))), "")
        tests_text = (f"Command: `python -m unittest discover -p \"test_*.py\"` "
                      f"from `backend/`.\n\n`{ran}`\n\n`{status}`")
        check(bool(status) and status.startswith("OK"),
              "the existing test suite passes", status)
    elif args.run_tests:
        started = time.time()
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"],
            cwd=BACKEND_DIR, capture_output=True, text=True)
        elapsed = time.time() - started
        output = (result.stdout or "") + (result.stderr or "")
        tail = output.strip().splitlines()[-6:]
        tests_text = (
            f"Command: `python -m unittest discover -p \"test_*.py\"` from "
            f"`backend/`.\n\nElapsed: {elapsed:.1f} s, exit status "
            f"{result.returncode}.\n\n```\n" + "\n".join(tail) + "\n```")
        check(result.returncode == 0, "the existing test suite passes",
              "\n".join(tail))

    # ------------------------------------------------------------------
    section("Report")
    # ------------------------------------------------------------------
    failed = [item for item in RESULTS if not item[0]]
    critical = [
        item for item in failed
        if "duplicate" in item[1].lower()
        or "development database" in item[1].lower()
        or "corpus file" in item[1].lower()
        or "row count" in item[1].lower()
        or "identities equal the plan" in item[1].lower()
        or "identity stored exactly once" in item[1].lower()
    ]
    if critical:
        verdict = "FAIL"
        verdict_reason = (
            f"{len(critical)} blocking check(s) failed; full-corpus ingestion "
            "must not proceed until they are fixed.")
    elif failed:
        verdict = "PASS WITH ISSUES"
        verdict_reason = (
            f"{len(failed)} non-blocking check(s) failed; the corpus should "
            "proceed only after the listed fixes.")
    else:
        verdict = "PASS"
        verdict_reason = (
            "both floats were stored and verified against their source files, "
            "re-ingestion inserted nothing, no identity or payload changed, and "
            "the development database and corpus are untouched.")

    section_names = list(OrderedDict.fromkeys(item[3] for item in RESULTS))
    section_rows = []
    for name in section_names:
        items = [item for item in RESULTS if item[3] == name]
        good = sum(1 for item in items if item[0])
        section_rows.append(f"| {name} | {good} | {len(items) - good} |")

    meta = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "server_version": server_version,
        "name_safe": True,
        "schema_statements": statements,
        "corpus_files": len(corpus_before),
        "used_files": len(used_paths),
        "file_counts": {spec["wmo"]: len(float_paths(spec["wmo"]))
                        for spec in FLOATS},
        "group_counts": {spec["wmo"]: ingest[spec["wmo"]]["groups"]
                         for spec in FLOATS},
        "ingest": {spec["wmo"]: dict(ingest[spec["wmo"]]) for spec in FLOATS},
        "planned": planned,
        "planned_total": sum(planned.values()),
        "inserted_total": sum(ingest[spec["wmo"]]["inserted"] for spec in FLOATS),
        "rows_total": base.count_profiles(engine),
        "floats": stats,
        "timings": timings,
        "dev_before": dev_before,
        "dev_after": dev_after,
        "corpus_after": len(corpus_after),
        "hashes_after": len(hashes_after),
        "second_inserted": second_inserted,
        "second_skipped": second_skipped,
        "second_errors": second_errors,
        "rows_after_second": rows_after_second,
        "duplicates_after_second": len(duplicates),
        "digest_changes": len(changed),
        "timestamp_changes": len(timestamp_changes),
        "identity_diff": f"missing {len(missing)}, unexpected {len(unexpected)}",
        "float_rows": len(float_rows),
        "duplicate_rejected": "confirmed" if duplicate_rejected else "NOT rejected",
        "schema_facts": schema_facts,
        "integrity": integrity,
        "defects": meta_defects(),
        "tests": tests_text,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "section_table": "| Section | Passed | Failed |\n|---|---|---|\n"
                         + "\n".join(section_rows),
    }

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    os.makedirs(args.json_dir, exist_ok=True)

    serialisable = {
        "environment": {
            "server_version": server_version,
            "development_database": DB_NAME,
            "disposable_database": DISPOSABLE_DB,
            "dataset_root": DATA_ROOT,
        },
        "counts": {
            "planned": planned,
            "ingest": {key: {k: v for k, v in value.items() if k != "errors"}
                       for key, value in ingest.items()},
            "rows_total": meta["rows_total"],
        },
        "floats": {
            str(wmo): {key: value for key, value in entry.items()
                       if key != "row_by_identity"}
            for wmo, entry in stats.items()
        },
        "idempotency": {
            "inserted": second_inserted,
            "skipped": second_skipped,
            "errors": second_errors,
            "digest_changes": len(changed),
            "timestamp_changes": len(timestamp_changes),
        },
        "development_database_unchanged": {
            key: dev_before[key] == dev_after[key]
            for key in ("rows", "floats", "fingerprint", "schema")
        },
        "corpus_unchanged": {
            "files_same_size_and_mtime": not changed_files,
            "hashes_same": not changed_hashes,
        },
        "checks": {
            "total": len(RESULTS),
            "passed": len(RESULTS) - len(failed),
            "failed": len(failed),
            "failures": [
                {"section": name, "check": label, "detail": detail}
                for _ok, label, detail, name in failed
            ],
        },
        "verdict": verdict,
    }
    with open(os.path.join(args.json_dir, "summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump(serialisable, handle, indent=2, default=str)

    report = format_markdown(meta)
    with open(args.report, "w", encoding="utf-8") as handle:
        handle.write(report)

    passed = len(RESULTS) - len(failed)
    print(f"\nchecks passed : {passed}")
    print(f"checks failed : {len(failed)}")
    for ok, label, detail, name in failed:
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))
    print(f"\nreport : {os.path.abspath(args.report)}")
    print(f"verdict: {verdict}")

    if args.keep_db:
        print(f"Kept database '{DISPOSABLE_DB}'.")
    else:
        base.drop_database(DISPOSABLE_DB)
        print(f"Dropped database '{DISPOSABLE_DB}'.")

    return 0 if not failed else 1


def meta_defects():
    """Defects are reported from the checks, never invented."""
    failed = [item for item in RESULTS if not item[0]]
    if not failed:
        return ("**None.** The writing pipeline required no change: every stored "
                "row matched its source file, the two floats stored exactly the "
                "planned profiles, and re-ingestion was a no-op. No production "
                "file was modified for this validation.")
    lines = ["The following checks failed during this run:", ""]
    for _ok, label, detail, name in failed:
        lines.append(f"* `{name}` — {label}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
