"""Recover the DIRECTION of argo_profiles rows added by migration V4.

V4 gives every existing row ``direction = '-'`` because direction was never
stored. For rows whose ``source_file`` is still on disk, the value can be read
back from the NetCDF file at the stored ``profile_index_in_file``. Nothing is
guessed: a row whose file is missing, whose N_PROF index no longer exists, whose
direction is not A/D, or whose observation time does not match the stored
``profile_time`` keeps ``'-'`` and is reported.

Only the ``direction`` column is written. No measurement, provenance or
``profile_time`` value is changed, and no row is inserted or deleted.

Run from ``backend/``:

    python scripts/backfill_direction.py --dry-run
    python scripts/backfill_direction.py
    python scripts/backfill_direction.py --data-dir "bgc data/bgc raw data"
"""

import argparse
import os
import sys
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bgc_matching import index_file, profile_time_from_juld  # noqa: E402
from config import (  # noqa: E402
    ARGO_DATA_DIR, DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER,
)


def build_file_index(data_dir):
    """Lower-cased file name -> every path with that name under ``data_dir``."""
    index = {}
    for root, _directories, filenames in os.walk(data_dir):
        for filename in filenames:
            if filename.lower().endswith(".nc"):
                index.setdefault(filename.lower(), []).append(
                    os.path.join(root, filename))
    return index


def direction_for(row, file_index, cache):
    """The file's DIRECTION for this row, or ``(None, reason)``."""
    name = row["source_file"]
    if not name:
        return None, "no source_file recorded"

    paths = file_index.get(str(name).lower())
    if not paths:
        return None, f"{name} not found under the data directory"
    if len(paths) > 1:
        return None, f"{name} is ambiguous ({len(paths)} copies)"

    path = paths[0]
    if path not in cache:
        try:
            cache[path] = index_file(path)
        except Exception as error:  # unreadable file: reported, never guessed
            cache[path] = error
    records = cache[path]
    if isinstance(records, Exception):
        return None, f"{name} could not be read: {records}"

    match = [
        record for record in records
        if record["index"] == int(row["profile_index_in_file"])
    ]
    if not match:
        return None, f"{name} has no N_PROF {row['profile_index_in_file']}"

    record = match[0]
    direction = record.get("direction")
    if direction not in ("A", "D"):
        return None, f"{name} N_PROF {record['index']} has no A/D direction"

    expected = profile_time_from_juld(record["juld"])
    if expected is not None and expected != row["profile_time"]:
        return None, (
            f"{name} N_PROF {record['index']} is observed at {expected}, "
            f"the row stores {row['profile_time']}"
        )
    return direction, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DB_NAME,
                        help="Database to update (default: DB_NAME).")
    parser.add_argument("--data-dir", default=ARGO_DATA_DIR,
                        help="Directory searched for the recorded source files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    parser.add_argument("--all", action="store_true",
                        help="Also check rows that already have a direction.")
    args = parser.parse_args()

    if not os.path.isdir(args.data_dir):
        raise SystemExit(f"Data directory not found: {args.data_dir}")

    url = (f"mysql+mysqlconnector://{quote_plus(DB_USER)}:"
           f"{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{args.database}")
    engine = create_engine(url)

    where = "" if args.all else "WHERE direction = '-'"
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT profile_id, cycle_number, profile_index_in_file,
                   profile_time, direction, source_file, source_file_sha256
            FROM argo_profiles {where}
        """)).mappings().fetchall()

    print(f"Database  : {args.database}")
    print(f"Data dir  : {args.data_dir}")
    print(f"Rows      : {len(rows)}{' (all rows)' if args.all else ' with direction = \'-\''}")

    file_index = build_file_index(args.data_dir)
    cache = {}
    updates = []
    unresolved = []
    unchanged = 0

    for row in rows:
        direction, reason = direction_for(row, file_index, cache)
        if direction is None:
            unresolved.append((row, reason))
            continue
        if direction == row["direction"]:
            unchanged += 1
            continue
        updates.append((row["profile_id"], direction))

    if updates and not args.dry_run:
        with engine.begin() as conn:
            for profile_id, direction in updates:
                conn.execute(
                    text("UPDATE argo_profiles SET direction = :d "
                         "WHERE profile_id = :pid"),
                    {"d": direction, "pid": profile_id},
                )

    print(f"Recovered : {len(updates)}"
          + (" (dry run, nothing written)" if args.dry_run else ""))
    print(f"Unchanged : {unchanged}")
    print(f"Unresolved: {len(unresolved)}")

    for row, reason in unresolved[:20]:
        print(f"  profile_id {row['profile_id']}: {reason}")
    if len(unresolved) > 20:
        print(f"  ... and {len(unresolved) - 20} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
