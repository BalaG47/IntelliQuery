"""Controlled unified core + BGC ingestion check against disposable databases.

This is a verification tool, not a test module. It never connects to ``DB_NAME``
except to read it as the source of a throw-away copy, and it writes no data to
it. Three throw-away databases are used:

  ``intelliquery_bgc_ingest_check``
      Built from the base schema plus V2, V3 and V4, then ingested with the
      production ``ArgoDataProcessor`` / ``DatabaseManager`` code paths.

  ``intelliquery_v4_migration_copy``
      A copy of the current development database, used to prove that V4 can be
      applied to real data: same row count, same payloads, no identity
      collision, then the direction backfill.

The raw dataset is opened read-only. Nothing is written to the development
database, ChromaDB or the corpus, and no bulk ingestion happens.

Run from ``backend/``:

    python scripts/bgc_controlled_ingestion.py
    python scripts/bgc_controlled_ingestion.py --keep-db

Exit status is 0 only when every check passes.
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np
import xarray as xr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from urllib.parse import quote_plus

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from config import (  # noqa: E402
    ARGO_DATA_DIR, DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER,
)
from data_processor import ArgoDataProcessor  # noqa: E402
from database_manager import DatabaseManager  # noqa: E402

TEMP_DB = "intelliquery_bgc_ingest_check"
COPY_DB = "intelliquery_v4_migration_copy"
DATA_ROOT = os.environ.get(
    "BGC_DATA_DIR", os.path.join(BACKEND_DIR, "bgc data", "bgc raw data"))
if not os.path.isabs(DATA_ROOT):
    DATA_ROOT = os.path.join(BACKEND_DIR, DATA_ROOT)

MIGRATIONS = (
    "V2__add_qc_adjusted_provenance_and_bgc_columns.sql",
    "V3__profile_identity_and_optional_position.sql",
    "V4__logical_profile_identity_and_bgc_axis.sql",
)

CORE = ("PRES", "TEMP", "PSAL")
CORE_AUXILIARY_PREFIXES = ("TEMP_STD", "PSAL_STD", "PRES_MED", "TEMP_MED",
                           "PSAL_MED")
CORE_AUXILIARY_EXACT = ("MTIME",)
FILL_VALUES = (99999.0, 60000.0, -99999.0)
GRID_STATUSES = {"ok", "pres_scale_suspect", "missing_pressure", "grid_mismatch"}

# Fourteen files producing eleven logical profiles:
#   float 3902581 cycle 1: ascending + descending, core and BGC      -> 2 rows
#   float 2902306 cycle 1: core and the rescaled-pressure BGC file   -> 1 row
#   seven synthetic-BGC-only files without a core counterpart        -> 7 rows
#   R6990679_001: four core variants of one profile                  -> 1 row
#   total                                                            -> 11 rows
INGEST_FILES = [
    # float 3902581, ascending and descending profiles of one cycle
    "incois/3902581/profiles/D3902581_001.nc",
    "incois/3902581/profiles/D3902581_001D.nc",
    "incois/3902581/profiles/SD3902581_001.nc",
    "incois/3902581/profiles/SD3902581_001D.nc",
    # the known rescaled-PRES anomaly, matched with its core product
    "incois/2902306/profiles/D2902306_001.nc",
    "incois/2902306/profiles/SD2902306_001.nc",
    # BGC-only profiles (no core file of these cycles is supplied)
    "incois/2902306/profiles/SD2902306_003.nc",
    "incois/2902306/profiles/SD2902306_004.nc",
    "incois/7902190/profiles/SD7902190_019.nc",
    "incois/7902170/profiles/SD7902170_001.nc",
    "incois/2903951/profiles/SR2903951_001.nc",
    "incois/7902200/profiles/SR7902200_015.nc",
    "incois/4903783/profiles/SD4903783_005.nc",
    # four N_PROF variants of one core profile
    "incois/6990679/profiles/R6990679_001.nc",
]
EXPECTED_ROWS = 11
EXPECTED_PROFILES_PER_FILE = {
    "D3902581_001.nc": 1,
    "D3902581_001D.nc": 1,
    "SD3902581_001.nc": 0,
    "SD3902581_001D.nc": 0,
    "D2902306_001.nc": 1,
    "SD2902306_001.nc": 0,
    "SD2902306_003.nc": 1,
    "SD2902306_004.nc": 1,
    "SD7902190_019.nc": 1,
    "SD7902170_001.nc": 1,
    "SR2903951_001.nc": 1,
    "SR7902200_015.nc": 1,
    "SD4903783_005.nc": 1,
    "R6990679_001.nc": 1,
}

# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------

RESULTS = []
CURRENT_SECTION = "general"


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, detail, CURRENT_SECTION))
    return bool(ok)


def section(title):
    global CURRENT_SECTION
    CURRENT_SECTION = title
    print("\n" + title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
# Source-side helpers (independent of the ingestion code, on purpose)
# ---------------------------------------------------------------------------


def decode_text(raw):
    if raw is None:
        return None
    if isinstance(raw, (bytes, np.bytes_)):
        text_value = raw.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
    else:
        text_value = str(raw).strip("\x00 \t\r\n")
    return text_value or None


def load(relpath):
    with xr.open_dataset(os.path.join(DATA_ROOT, relpath),
                         decode_times=False) as dataset:
        return dataset.load()


def station_names(profile):
    if "STATION_PARAMETERS" not in profile.variables:
        return []
    names = []
    for item in np.atleast_1d(profile["STATION_PARAMETERS"].values):
        name = decode_text(item)
        if name:
            names.append(name.upper())
    return names


def source_values(profile, var):
    if var not in profile.variables:
        return None
    values = []
    for raw in np.asarray(profile[var].values).ravel():
        if isinstance(raw, (bytes, np.bytes_, str, np.str_)):
            text_value = decode_text(raw)
            values.append(text_value if text_value in {
                "0", "1", "2", "3", "4", "5", "8", "9"} else None)
            continue
        number = float(raw)
        if np.isnan(number) or number in FILL_VALUES:
            values.append(None)
        else:
            values.append(round(number, 4))
    return values


def expected_product_type(filename):
    for prefix, name in (("SD", "synthetic_delayed"), ("SR", "synthetic_realtime"),
                         ("BD", "bgc_delayed"), ("BR", "bgc_realtime"),
                         ("D", "core_delayed"), ("R", "core_realtime")):
        if filename.startswith(prefix):
            return name
    return "unknown"


def is_core_auxiliary(name):
    return (name in CORE_AUXILIARY_EXACT
            or name.startswith(CORE_AUXILIARY_PREFIXES))


# ---------------------------------------------------------------------------
# Disposable databases
# ---------------------------------------------------------------------------


def server_engine():
    url = (f"mysql+mysqlconnector://{quote_plus(DB_USER)}:"
           f"{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/")
    return create_engine(url)


def engine_for(database):
    url = (f"mysql+mysqlconnector://{quote_plus(DB_USER)}:"
           f"{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{database}")
    return create_engine(url)


def run_sql_file(engine, path):
    with open(path, encoding="utf-8") as handle:
        sql = handle.read()
    lines = [line for line in sql.splitlines()
             if not line.strip().startswith("--")]
    statements = [part.strip() for part in "\n".join(lines).split(";")
                  if part.strip()]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return len(statements)


class DisposableDatabaseManager(DatabaseManager):
    """Real schema/insert logic, but only against a disposable database.

    Skips ``DatabaseManager.__init__`` so no ChromaDB store and no embedding
    model is touched; ChromaDB writes are recorded instead.
    """

    def __init__(self, engine):
        self.mysql_engine = engine
        self.chroma_writes = []

    def add_profile_to_chromadb(self, profile_id, float_id, cycle, time,
                               lat, lon, *args, **kwargs):
        self.chroma_writes.append((profile_id, float_id, cycle, lat, lon))


def reset_database(database):
    with server_engine().begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        conn.execute(text(f"CREATE DATABASE `{database}`"))


def drop_database(database):
    with server_engine().begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))


def build_schema(manager):
    manager.create_mysql_tables()
    count = 0
    for name in MIGRATIONS:
        count += run_sql_file(manager.mysql_engine,
                              os.path.join(BACKEND_DIR, "migrations", name))
    return count


def fetch_rows(engine):
    # No ORDER BY: the row payload (several MB of JSON) exceeds MySQL's default
    # sort buffer. Ordering is done in Python instead.
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.profile_id, f.wmo_number, p.cycle_number, p.direction,
                   p.profile_index_in_file, p.profile_time, p.latitude,
                   p.longitude, p.data_mode, p.source_file, p.source_metadata,
                   p.pressure, p.temperature, p.salinity, p.bgc_params,
                   p.bgc_pressure, p.bgc_source_metadata
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
        """)).mappings().fetchall()
    return sorted(rows, key=lambda row: row["profile_id"])


def count_profiles(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM argo_profiles")).scalar()


def duplicate_identities(engine):
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT float_id, cycle_number, direction, profile_time, COUNT(*) n
            FROM argo_profiles
            GROUP BY float_id, cycle_number, direction, profile_time
            HAVING n > 1
        """)).fetchall()


def payload_fingerprint(engine):
    """A value that only stays equal when every stored payload is unchanged."""
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT COUNT(*) AS rows_total,
                   COALESCE(SUM(CRC32(CONCAT_WS('|', profile_id, float_id,
                       cycle_number, profile_time, latitude, longitude,
                       data_mode, source_file))), 0) AS identity_hash,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(pressure AS CHAR)), 0)), 0)
                       AS pres,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(temperature AS CHAR)), 0)), 0)
                       AS temp,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(salinity AS CHAR)), 0)), 0)
                       AS psal,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(bgc_params AS CHAR)), 0)), 0)
                       AS bgc
            FROM argo_profiles
        """)).mappings().fetchone()


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------


def verify_schema(manager):
    columns = manager._get_profile_table_columns()
    check(len(columns) == 39,
          "argo_profiles has the 36 pre-V4 columns plus 3 new ones",
          f"{len(columns)} columns")
    check({"direction", "bgc_pressure", "bgc_source_metadata"} <= columns,
          "direction / bgc_pressure / bgc_source_metadata exist")

    keys = manager._get_profile_unique_keys()
    check(keys == (DatabaseManager.PROFILE_IDENTITY_KEY,),
          "the only unique key is the logical identity",
          str(keys))
    check(keys == (("float_id", "cycle_number", "direction", "profile_time"),),
          "logical identity is (float_id, cycle_number, direction, profile_time)")

    with manager.mysql_engine.connect() as conn:
        nullability = dict(conn.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'argo_profiles' "
            "AND COLUMN_NAME IN ('latitude', 'longitude', 'direction', "
            "'bgc_pressure')")).fetchall())
    check(nullability.get("latitude") == "YES"
          and nullability.get("longitude") == "YES",
          "latitude/longitude accept NULL", str(nullability))
    check(nullability.get("direction") == "NO",
          "direction is NOT NULL (a NULL column would not constrain a key)",
          str(nullability))

    manager._validate_profile_schema({
        "direction": "A", "bgc_pressure": None, "bgc_source_metadata": None,
        "float_id": 1, "cycle_number": 1, "profile_time": None,
    })


# ---------------------------------------------------------------------------
# 2. Migration V4 on a copy of the development database
# ---------------------------------------------------------------------------


def copy_development_database():
    with server_engine().begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{COPY_DB}`"))
        conn.execute(text(f"CREATE DATABASE `{COPY_DB}`"))
        for table in ("argo_floats", "argo_profiles"):
            conn.execute(text(
                f"CREATE TABLE `{COPY_DB}`.`{table}` LIKE "
                f"`{DB_NAME}`.`{table}`"))
            conn.execute(text(
                f"INSERT INTO `{COPY_DB}`.`{table}` SELECT * FROM "
                f"`{DB_NAME}`.`{table}`"))


def verify_migration_copy():
    copy_development_database()
    engine = engine_for(COPY_DB)

    before = payload_fingerprint(engine)
    check(before["rows_total"] > 0,
          "the development database copy holds rows", str(before["rows_total"]))

    with engine.connect() as conn:
        collisions = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT float_id, cycle_number, profile_time
                FROM argo_profiles
                GROUP BY float_id, cycle_number, profile_time
                HAVING COUNT(*) > 1
            ) t
        """)).scalar()
    check(collisions == 0,
          "no existing row collides on (float, cycle, profile_time)",
          f"{collisions} collisions")

    run_sql_file(engine, os.path.join(
        BACKEND_DIR, "migrations",
        "V4__logical_profile_identity_and_bgc_axis.sql"))

    after = payload_fingerprint(engine)
    check(after["rows_total"] == before["rows_total"],
          "V4 changed no row count",
          f"{before['rows_total']} -> {after['rows_total']}")
    for field, label in (("identity_hash", "identity columns"),
                        ("pres", "pressure payload"),
                        ("temp", "temperature payload"),
                        ("psal", "salinity payload"),
                        ("bgc", "BGC payload")):
        check(after[field] == before[field],
              f"V4 left every {label} value unchanged",
              f"{before[field]} -> {after[field]}")

    manager = DisposableDatabaseManager(engine)
    verify_schema(manager)

    with engine.connect() as conn:
        legacy = conn.execute(text(
            "SELECT COUNT(*) FROM argo_profiles WHERE direction = '-'")).scalar()
    check(legacy == before["rows_total"],
          "every legacy row starts as direction '-' (unknown, not guessed)",
          f"{legacy} rows")

    # The new key must accept an ascending/descending pair of one cycle and
    # reject a second row with the same identity. Everything is rolled back.
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            float_id = conn.execute(text(
                "SELECT float_id FROM argo_floats LIMIT 1")).scalar()
            insert = text("""
                INSERT INTO argo_profiles
                    (float_id, cycle_number, direction, profile_time)
                VALUES (:fid, 9001, :dir, '2001-01-01 00:00:00')
            """)
            conn.execute(insert, {"fid": float_id, "dir": "A"})
            conn.execute(insert, {"fid": float_id, "dir": "D"})
            check(True, "the new key accepts ascending + descending of one cycle")
            rejected = False
            try:
                conn.execute(insert, {"fid": float_id, "dir": "A"})
            except IntegrityError:
                rejected = True
            check(rejected, "the new key rejects a duplicate logical identity")
        finally:
            trans.rollback()

    # Backfill direction from the source files recorded on each row.
    result = subprocess.run(
        [sys.executable, os.path.join(BACKEND_DIR, "scripts",
                                      "backfill_direction.py"),
         "--database", COPY_DB, "--data-dir", ARGO_DATA_DIR],
        capture_output=True, text=True, cwd=BACKEND_DIR,
    )
    print((result.stdout or "").strip())
    check(result.returncode == 0, "the direction backfill ran",
          (result.stderr or "").strip()[-400:])

    with engine.connect() as conn:
        remaining = conn.execute(text(
            "SELECT COUNT(*) FROM argo_profiles WHERE direction = '-'")).scalar()
        recovered = conn.execute(text(
            "SELECT COUNT(*) FROM argo_profiles WHERE direction IN ('A', 'D')"
        )).scalar()
    check(recovered + remaining == before["rows_total"],
          "the backfill neither added nor removed a row",
          f"{recovered} recovered, {remaining} still unknown")
    check(recovered > 0, "legacy directions were recovered from the sources",
          f"{recovered}/{before['rows_total']}")
    check(remaining == 0,
          "every legacy row had a recoverable direction",
          f"{remaining} unresolved")

    after_backfill = payload_fingerprint(engine)
    check(after_backfill["rows_total"] == before["rows_total"],
          "backfill left the row count unchanged")

    drop_database(COPY_DB)


# ---------------------------------------------------------------------------
# 3-4. Group ingestion and merged-row verification
# ---------------------------------------------------------------------------


def expected_profiles():
    """(file, wmo, cycle, direction of every profile the sample should store)."""
    return [
        ("D3902581_001.nc", 3902581, 1, "A"),
        ("D3902581_001D.nc", 3902581, 1, "D"),
        ("D2902306_001.nc", 2902306, 1, "A"),
        ("SD2902306_003.nc", 2902306, 3, None),
        ("SD2902306_004.nc", 2902306, 4, None),
        ("SD7902190_019.nc", 7902190, 19, None),
        ("SD7902170_001.nc", 7902170, 1, None),
        ("SR2903951_001.nc", 2903951, 1, None),
        ("SR7902200_015.nc", 7902200, 15, None),
        ("SD4903783_005.nc", 4903783, 5, None),
        ("R6990679_001.nc", 6990679, 1, None),
    ]


def dataset_for(cache, filename):
    if filename not in cache:
        for relpath in INGEST_FILES:
            if os.path.basename(relpath) == filename:
                cache[filename] = load(relpath)
                break
    return cache[filename]


def verify_rows(rows, datasets):
    check(len(rows) == EXPECTED_ROWS,
          "one row per logical profile",
          f"{len(rows)} rows, expected {EXPECTED_ROWS}")
    check(not duplicate_identities(engine_for(TEMP_DB)),
          "no duplicate logical identity")

    identities = {(r["wmo_number"], r["cycle_number"], r["direction"],
                   r["profile_time"]) for r in rows}
    check(len(identities) == len(rows),
          "every (float, cycle, direction, time) is stored exactly once")

    by_source = {}
    for row in rows:
        if row["source_file"]:
            by_source.setdefault(row["source_file"], 0)
            by_source[row["source_file"]] += 1
    check(by_source, "core source files are recorded", str(sorted(by_source)))

    for filename, expected in EXPECTED_PROFILES_PER_FILE.items():
        actual = by_source.get(filename, 0)
        check(actual == expected, f"{filename} contributes {expected} row(s)",
              f"{actual}")

    for row in rows:
        label = (f"{row['wmo_number']} cycle {row['cycle_number']} "
                 f"{row['direction']}")
        check(row["direction"] in ("A", "D"),
              f"{label}: direction is a real Argo direction",
              str(row["direction"]))
        check(row["source_metadata"] is not None,
              f"{label}: source_metadata present")

        metadata = json.loads(row["source_metadata"] or "{}")
        variants = metadata.get("core_variants")
        if row["source_file"] and row["source_file"].startswith(
                ("D", "R")):
            check(isinstance(variants, list) and variants,
                  f"{label}: core variants recorded")
            selected = [v for v in variants if v.get("selected")]
            check(len(selected) == 1,
                  f"{label}: exactly one core variant is selected",
                  str(len(selected)))

            profile = dataset_for(datasets, row["source_file"]).isel(
                N_PROF=row["profile_index_in_file"])
            for name, column in (("PRES", "pressure"),
                                 ("TEMP", "temperature"),
                                 ("PSAL", "salinity")):
                check(json.loads(row[column]) == source_values(profile, name),
                      f"{label}: core {name} equals the selected variant")

        if row["bgc_source_metadata"] is None:
            check(row["bgc_pressure"] is None,
                  f"{label}: no BGC axis without BGC measurements")
            continue

        bgc_metadata = json.loads(row["bgc_source_metadata"])
        bgc_file = bgc_metadata["source_file"]
        if bgc_metadata["match"] == "matched":
            check(bgc_file != row["source_file"],
                  f"{label}: the BGC product is a separate source file", bgc_file)
        else:
            check(bgc_file == row["source_file"],
                  f"{label}: the BGC-only row is its own source", bgc_file)
        check(bgc_metadata["grid_check"] in GRID_STATUSES,
              f"{label}: grid check status recorded",
              str(bgc_metadata["grid_check"]))

        bgc_profile = dataset_for(datasets, bgc_file).isel(
            N_PROF=bgc_metadata["profile_index_in_file"])
        bgc_pressure = json.loads(row["bgc_pressure"])
        check(bgc_pressure == source_values(bgc_profile, "PRES"),
              f"{label}: BGC pressure is the BGC product's own axis")

        bgc_params = json.loads(row["bgc_params"])
        expected_names = {
            name for name in station_names(bgc_profile)
            if name not in CORE and not is_core_auxiliary(name)
            and name in bgc_profile.variables
        }
        check(set(bgc_params) == expected_names,
              f"{label}: BGC parameter set matches STATION_PARAMETERS",
              f"{sorted(bgc_params)} vs {sorted(expected_names)}")
        for name, values in bgc_params.items():
            check(values == source_values(bgc_profile, name),
                  f"{label}: BGC {name} matches its source")
            check(len(values) == len(bgc_pressure),
                  f"{label}: {name} and its axis have the same length")

        if row["source_file"] and bgc_metadata["match"] == "matched":
            core_pressure = [v for v in json.loads(row["pressure"])
                             if v is not None]
            detail = bgc_metadata["grid_check_detail"]
            if bgc_metadata["grid_check"] == "ok":
                check(detail["overlap_fraction"] >= 0.9,
                      f"{label}: the core grid is inside the BGC grid",
                      str(detail["overlap_fraction"]))
                check(min(core_pressure) >= detail["bgc_pressure_min"]
                      and max(core_pressure) <= detail["bgc_pressure_max"]
                      or detail["overlap_fraction"] >= 0.9,
                      f"{label}: the core axis is covered by the BGC axis")
            elif bgc_metadata["grid_check"] == "pres_scale_suspect":
                check(detail["median_ratio"] > 100,
                      f"{label}: the rescaling is reported, not applied",
                      str(detail["median_ratio"]))
                check(max(core_pressure) > 1000,
                      f"{label}: the core pressure was kept unscaled")
            else:
                check(False, f"{label}: unexpected grid status",
                      str(bgc_metadata["grid_check"]))


def verify_rescaled_anomaly(rows):
    suspects = [
        row for row in rows
        if row["bgc_source_metadata"]
        and json.loads(row["bgc_source_metadata"])["grid_check"]
        == "pres_scale_suspect"
    ]
    check(len(suspects) == 1,
          "SD2902306_001 is flagged pres_scale_suspect and nothing else is",
          f"{len(suspects)} flagged")
    if suspects:
        check(suspects[0]["bgc_source_metadata"] is not None
              and "SD2902306_001.nc" in suspects[0]["bgc_source_metadata"],
              "the flagged row is the known SD2902306_001 defect")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true",
                        help="Leave the disposable database in place.")
    args = parser.parse_args()

    if TEMP_DB == DB_NAME or COPY_DB == DB_NAME:
        raise SystemExit("Refusing to run: a disposable database name matches "
                         "DB_NAME.")
    if not os.path.isdir(DATA_ROOT):
        raise SystemExit(f"Dataset not found: {DATA_ROOT}")
    missing = [rel for rel in INGEST_FILES
               if not os.path.isfile(os.path.join(DATA_ROOT, rel))]
    if missing:
        raise SystemExit("Missing sample file(s): " + ", ".join(missing))

    print(f"Disposable database : {TEMP_DB}")
    print(f"Migration copy      : {COPY_DB}")
    print(f"Dataset (read-only) : {DATA_ROOT}")
    print(f"Development DB      : {DB_NAME} (read as the copy source only)")

    section("1. Disposable database and V4 schema")
    reset_database(TEMP_DB)
    engine = engine_for(TEMP_DB)
    manager = DisposableDatabaseManager(engine)
    statements = build_schema(manager)
    print(f"applied {statements} migration statements")
    verify_schema(manager)

    section("2. V4 migration on a copy of the development database")
    verify_migration_copy()

    section("3. Controlled group ingestion (first pass)")
    processor = ArgoDataProcessor(manager)
    first = processor.process_paths(
        [os.path.join(DATA_ROOT, rel) for rel in INGEST_FILES])
    print(f"files={first['files']} groups={first['groups']} "
          f"inserted={first['inserted']} skipped={first['skipped']} "
          f"errors={len(first['errors'])}")
    for error in first["errors"]:
        print(f"  ERROR {error['file']}: {error['error']}")
    check(not first["errors"], "no file or group reported an error",
          str(first["errors"])[:400])
    check(first["inserted"] == EXPECTED_ROWS,
          "first pass inserted one row per logical profile",
          f"{first['inserted']}/{EXPECTED_ROWS}")
    check(first["skipped"] == 0, "first pass skipped nothing")
    check(len(manager.chroma_writes) == EXPECTED_ROWS,
          "one ChromaDB document per logical profile",
          f"{len(manager.chroma_writes)}")

    rows = fetch_rows(engine)
    datasets = {}
    section("4. Stored rows against the NetCDF sources")
    verify_rows(rows, datasets)
    verify_rescaled_anomaly(rows)

    section("5. Re-ingestion (idempotency)")
    second = processor.process_paths(
        [os.path.join(DATA_ROOT, rel) for rel in INGEST_FILES])
    check(second["inserted"] == 0, "second pass inserted nothing",
          f"inserted={second['inserted']}")
    check(second["skipped"] == EXPECTED_ROWS,
          "second pass skipped every stored logical profile",
          f"skipped={second['skipped']}")
    check(count_profiles(engine) == EXPECTED_ROWS,
          "row count unchanged after re-ingestion",
          f"{count_profiles(engine)} rows")
    check(not duplicate_identities(engine),
          "still no duplicate identities after re-ingestion")

    section("6. Core-only ingestion still works")
    core_only_db = DisposableDatabaseManager(engine_for(TEMP_DB))
    core_only_processor = ArgoDataProcessor(core_only_db)
    core_result = core_only_processor.process_file(
        os.path.join(DATA_ROOT, "incois/3902581/profiles/R3902581_059.nc"))
    check(core_result["inserted"] == 1,
          "a core-only file still inserts one row", str(core_result))
    check(count_profiles(engine) == EXPECTED_ROWS + 1,
          "the controlled rows were not disturbed", f"{count_profiles(engine)}")

    section("SUMMARY")
    passed = sum(1 for item in RESULTS if item[0])
    failed = [item for item in RESULTS if not item[0]]
    print(f"checks passed : {passed}")
    print(f"checks failed : {len(failed)}")

    groups = {}
    for ok, _label, _detail, name in RESULTS:
        counts = groups.setdefault(name, [0, 0])
        counts[0 if ok else 1] += 1
    for name, (good, bad) in groups.items():
        print(f"  {good:>3} passed, {bad:>2} failed  {name}")

    for _, label, detail, _name in failed:
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))

    if args.keep_db:
        print(f"\nKept database '{TEMP_DB}' for inspection.")
    else:
        drop_database(TEMP_DB)
        print(f"\nDropped database '{TEMP_DB}'.")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
