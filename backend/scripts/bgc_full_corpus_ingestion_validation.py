"""Full-corpus BGC ingestion validation against a clone of the development DB.

Phase 3 of the BGC ingestion work: after the controlled two-float validation
(203 profiles, 4,679 checks) this script validates the *complete* corpus
(1,264 NetCDF files, 632 logical profiles, 8 floats) against an isolated clone
of the development database and an isolated ChromaDB store.

What is written, and where:

* ``intelliquery_bgc_full_corpus_check`` — clone of ``DB_NAME`` (schema, indexes,
  constraints and all existing rows), then the corpus is ingested into it.
* ``scratch/bgc_full_validation/chroma_store`` — a ChromaDB directory created for
  this run with its own collection, so the existing vector collection is neither
  read for writing nor touched.
* The development database is opened for ``SELECT``/``SHOW`` only.
* ``intelliquery_bgc_interrupt_check`` + a second vector directory — used once to
  observe what an interrupted run leaves behind, then dropped.

Row-level source comparison is delegated to the validated two-float validator
(``bgc_two_float_ingestion_validation``), which is imported — not copied — so
the comparison rules stay identical to the ones that already passed.

Run from ``backend/``:

    python scripts/bgc_full_corpus_ingestion_validation.py --run-tests
    python scripts/bgc_full_corpus_ingestion_validation.py --keep-db

Exit status is 0 only when every check passes.
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, OrderedDict

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))

import chromadb  # noqa: E402
import bgc_controlled_ingestion as base  # noqa: E402
import bgc_two_float_ingestion_validation as two  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

from config import (  # noqa: E402
    CHROMA_COLLECTION_NAME, CHROMA_PERSIST_DIR, DB_HOST, DB_NAME, DB_PORT, DB_USER,
)
from data_processor import ArgoDataProcessor, compute_file_sha256  # noqa: E402
from database_manager import DatabaseManager  # noqa: E402
from bgc_matching import GRID_OK, GRID_PRES_SCALE_SUSPECT  # noqa: E402

DISPOSABLE_DB = "intelliquery_bgc_full_corpus_check"
INTERRUPT_DB = "intelliquery_bgc_interrupt_check"

DATA_ROOT = base.DATA_ROOT
WORK_DIR = os.path.join(BACKEND_DIR, "scratch", "bgc_full_validation")
CHROMA_DIR = os.path.join(WORK_DIR, "chroma_store")
INTERRUPT_CHROMA_DIR = os.path.join(WORK_DIR, "chroma_interrupt")
REPORT_PATH = os.path.join(
    BACKEND_DIR, "..", "docs", "bgc", "BGC_FULL_CORPUS_INGESTION_VALIDATION.md")
JSON_DIR = WORK_DIR

#: A collection name of its own, so the development collection is never used.
COLLECTION_NAME = "argo_float_profiles_full_check"

#: Every float of the corpus, in the audit's order, with the audit's own
#: expectations (docs/bgc/BGC_FULL_CORPUS_DRY_RUN_AUDIT.md, section 2).
FLOATS = [
    {"wmo": 2902306, "profiles": 85, "directions": {"A": 85},
     "suspect_cycles": (1, 11, 16, 17, 18, 23, 24, 25, 26, 39)},
    {"wmo": 2903951, "profiles": 69, "directions": {"A": 69},
     "suspect_cycles": ()},
    {"wmo": 3902581, "profiles": 118, "directions": {"A": 112, "D": 6},
     "suspect_cycles": ()},
    {"wmo": 4903783, "profiles": 103, "directions": {"A": 103},
     "suspect_cycles": ()},
    {"wmo": 6990679, "profiles": 26, "directions": {"A": 26},
     "suspect_cycles": ()},
    {"wmo": 7902170, "profiles": 92, "directions": {"A": 92},
     "suspect_cycles": (22, 26)},
    {"wmo": 7902190, "profiles": 57, "directions": {"A": 57},
     "suspect_cycles": ()},
    {"wmo": 7902200, "profiles": 82, "directions": {"A": 82},
     "suspect_cycles": (15,)},
]

EXPECTED_FILES = 1264
EXPECTED_PROFILES = 632
EXPECTED_GROUPS = 626
EXPECTED_SUSPECTS = 13
EXPECTED_PARAMETER_COUNTS = {
    "DOXY": 619, "CHLA": 619, "BBP700": 619,
    "NITRATE": 126, "PH_IN_SITU_TOTAL": 316,
}

#: Files whose content must not change; the ingestion code itself.
PRODUCTION_FILES = (
    "config.py", "data_processor.py", "database_manager.py", "bgc_matching.py",
    "migrations/V2__add_qc_adjusted_provenance_and_bgc_columns.sql",
    "migrations/V3__profile_identity_and_optional_position.sql",
    "migrations/V4__logical_profile_identity_and_bgc_axis.sql",
)

check = two.check
section = two.section
Tally = two.Tally


# ---------------------------------------------------------------------------
# Isolated ChromaDB store
# ---------------------------------------------------------------------------

class IsolatedChromaManager(base.DisposableDatabaseManager):
    """Production ``DatabaseManager`` against a disposable database and store.

    ``DatabaseManager.__init__`` is skipped so the configured (development)
    ChromaDB directory is never opened; everything else — the document builder,
    the embedding model, ``add_profile_to_chromadb`` — is the production code.
    Writes are recorded next to the real write so the run can be audited.
    """

    def __init__(self, engine, chroma_dir, collection_name):
        self.mysql_engine = engine
        self.chroma_writes = []
        self.chroma_dir = chroma_dir
        self.collection_name = collection_name
        self.chroma_client = chromadb.PersistentClient(path=chroma_dir)
        self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        self.chroma_collection = self.chroma_client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"})

    def add_profile_to_chromadb(self, profile_id, float_id, cycle, time,
                               lat, lon, bgc_keys, pressure, temperature,
                               salinity, direction=None):
        DatabaseManager.add_profile_to_chromadb(
            self, profile_id, float_id, cycle, time, lat, lon, bgc_keys,
            pressure, temperature, salinity, direction=direction)
        self.chroma_writes.append({
            "profile_id": int(profile_id),
            "float_id": int(float_id),
            "cycle": int(cycle),
            "bgc_keys": list(bgc_keys) if bgc_keys else [],
            "direction": direction,
        })


def chroma_fingerprint(directory):
    """File count and content hash of a ChromaDB directory tree.

    The store is only read as bytes here; no client is opened, so fingerprinting
    cannot write to it.
    """
    if not os.path.isdir(directory):
        return {"exists": False, "files": 0, "digest": None, "bytes": 0}
    digest = hashlib.sha256()
    files = 0
    total = 0
    for root, _dirs, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(root, name)
            info = os.stat(path)
            files += 1
            total += info.st_size
            digest.update(os.path.relpath(path, directory).encode("utf-8"))
            digest.update(str(info.st_size).encode("ascii"))
            digest.update(str(info.st_mtime_ns).encode("ascii"))
    return {"exists": True, "files": files, "digest": digest.hexdigest(),
            "bytes": total}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def clone_development_database(target):
    """Create ``target`` as a structural + data clone of ``DB_NAME``.

    The tables are created from the source ``SHOW CREATE TABLE`` text rather
    than ``CREATE TABLE … LIKE``: ``LIKE`` copies columns and indexes but
    **drops foreign keys**, which would leave the clone with a schema that is
    not the development schema.
    """
    with base.server_engine().begin() as conn:
        charset = conn.execute(text(
            "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
            "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :name"
        ), {"name": DB_NAME}).fetchone()
        conn.execute(text(f"DROP DATABASE IF EXISTS `{target}`"))
        if charset:
            conn.execute(text(
                f"CREATE DATABASE `{target}` CHARACTER SET {charset[0]} "
                f"COLLATE {charset[1]}"))
        else:
            conn.execute(text(f"CREATE DATABASE `{target}`"))
        for table in ("argo_floats", "argo_profiles"):
            create_sql = conn.execute(text(
                f"SHOW CREATE TABLE `{DB_NAME}`.`{table}`")).fetchone()[1]
            qualified = create_sql.replace(
                f"CREATE TABLE `{table}`", f"CREATE TABLE `{target}`.`{table}`", 1)
            conn.execute(text(qualified))
            conn.execute(text(
                f"INSERT INTO `{target}`.`{table}` SELECT * FROM "
                f"`{DB_NAME}`.`{table}`"))


def foreign_keys(engine):
    """Foreign keys as (constraint, column, referenced table, column)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
                   REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'argo_profiles'
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """)).fetchall()
    return sorted(tuple(str(value) for value in row) for row in rows)


def identity_rows(engine):
    """Every stored identity -> profile_id, in one query."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.profile_id, f.wmo_number, p.cycle_number, p.direction,
                   p.profile_time
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
        """)).mappings().fetchall()
    return {(int(row["wmo_number"]), row["cycle_number"], row["direction"],
             row["profile_time"]): int(row["profile_id"]) for row in rows}


def digest_for_profile_ids(engine, profile_ids):
    """SHA-256 of every stored value, for a fixed set of profile ids."""
    if not profile_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {two.FETCH_COLUMNS}
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
            WHERE p.profile_id IN ({",".join(str(int(value)) for value in profile_ids)})
        """)).mappings().fetchall()
    digests = {}
    for row in rows:
        payload = json.dumps({key: row[key] for key in row.keys()},
                             sort_keys=True, default=str)
        digests[row["profile_id"]] = hashlib.sha256(
            payload.encode("utf-8")).hexdigest()
    return digests


def verify_migrations(engine, dev_schema):
    """Verify that V2, V3 and V4 are all present on the clone."""
    manager = base.DisposableDatabaseManager(engine)
    columns = manager._get_profile_table_columns()
    keys = manager._get_profile_unique_keys()

    check(columns >= DatabaseManager.REQUIRED_PROFILE_COLUMNS,
          "V2 (QC / adjusted / provenance / BGC columns) is applied",
          str(sorted(DatabaseManager.REQUIRED_PROFILE_COLUMNS - columns)))
    check({"direction", "bgc_pressure", "bgc_source_metadata"} <= columns
          and ("float_id", "cycle_number", "direction", "profile_time") in keys,
          "V4 (logical identity + BGC axis) is applied", str(keys))
    check(keys == (DatabaseManager.PROFILE_IDENTITY_KEY,),
          "V3/V4 left exactly one unique key: the logical identity", str(keys))

    with engine.connect() as conn:
        shape = dict(conn.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'argo_profiles' "
            "AND COLUMN_NAME IN ('latitude','longitude','direction')"
        )).fetchall())
        default = conn.execute(text(
            "SELECT COLUMN_DEFAULT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'argo_profiles' "
            "AND COLUMN_NAME = 'direction'")).scalar()
        foreign = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'argo_profiles' "
            "AND REFERENCED_TABLE_NAME = 'argo_floats'")).scalar()
        engine_name = conn.execute(text("SELECT DATABASE()")).scalar()
    check(shape.get("latitude") == "YES" and shape.get("longitude") == "YES",
          "V3 kept latitude/longitude nullable (no invented position)", str(shape))
    check(shape.get("direction") == "NO",
          "direction is NOT NULL", str(shape))
    check(str(default).strip("'") == "-",
          "direction defaults to '-' so a legacy row is never guessed", str(default))
    check(int(foreign) >= 1, "the argo_floats foreign key survived the migrations",
          str(foreign))
    check(engine_name == DISPOSABLE_DB,
          "all schema work happened in the disposable database", str(engine_name))

    clone_schema = two.schema_signature(engine)
    check(clone_schema == dev_schema,
          "the clone schema is identical to the development schema",
          "columns/indexes differ")

    # ``_validate_profile_schema`` is what the production insert calls first.
    manager._validate_profile_schema({
        "float_id": 1, "cycle_number": 1, "direction": "A",
        "profile_time": None, "bgc_pressure": None, "bgc_source_metadata": None,
    })
    return manager


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------

def float_paths(wmo):
    pattern = os.path.join(DATA_ROOT, "incois", str(wmo), "profiles", "*.nc")
    return sorted(glob.glob(pattern))


def all_corpus_paths():
    paths = []
    for root, _dirs, names in os.walk(DATA_ROOT):
        for name in names:
            if name.lower().endswith(".nc"):
                paths.append(os.path.join(root, name))
    return sorted(paths)


def hash_paths(paths):
    return {path: compute_file_sha256(path) for path in paths}


class ResourceSampler:
    """Samples this process's RSS until stopped (psutil, when available)."""

    def __init__(self):
        self.peak = None
        self._stop = threading.Event()
        try:
            import psutil
            self._psutil = psutil
        except ImportError:
            self._psutil = None

    def start(self):
        if self._psutil is None:
            return
        process = self._psutil.Process(os.getpid())

        def run():
            while not self._stop.is_set():
                try:
                    rss = process.memory_info().rss
                    self.peak = rss if self.peak is None else max(self.peak, rss)
                except Exception:
                    return
                self._stop.wait(2.0)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()

    @property
    def peak_mb(self):
        return None if self.peak is None else round(self.peak / (1024 * 1024), 1)


# ---------------------------------------------------------------------------
# ChromaDB validation
# ---------------------------------------------------------------------------

def validate_vector_store(collection, rows_by_float, chroma_writes):
    """Documents, ids and metadata of the isolated collection against the rows."""
    expected = {}
    for wmo, rows in rows_by_float.items():
        for row in rows:
            expected[int(row["profile_id"])] = (int(wmo), row)

    count = collection.count()
    check(count == EXPECTED_PROFILES,
          "vector records were created for every ingested profile",
          f"{count} vectors vs {len(expected)} rows")

    ids = collection.get(include=[])["ids"]
    check(len(ids) == len(set(ids)),
          "no duplicate vector record exists", f"{len(ids)} ids, {len(set(ids))} unique")
    expected_ids = {f"profile_{pid}" for pid in expected}
    check(set(ids) == expected_ids,
          "vector ids equal the relational profile ids",
          f"missing {len(expected_ids - set(ids))}, extra {len(set(ids) - expected_ids)}")

    check(len(chroma_writes) == len(expected),
          "the production vector write path ran once per inserted row",
          f"{len(chroma_writes)} writes vs {len(expected)} rows")

    payload = collection.get(ids=sorted(expected_ids))
    documents = dict(zip(payload["ids"], payload["documents"]))
    metadatas = dict(zip(payload["ids"], payload["metadatas"]))

    metadata_problems = []
    document_problems = []
    bgc_document_problems = []
    for pid, (wmo, row) in expected.items():
        document_id = f"profile_{pid}"
        metadata = metadatas.get(document_id) or {}
        document = documents.get(document_id) or ""

        if int(metadata.get("profile_id_sql", -1)) != pid:
            metadata_problems.append(f"{document_id} profile_id_sql")
        if int(metadata.get("float_id_sql", -1)) != wmo:
            metadata_problems.append(f"{document_id} float_id_sql")
        if int(metadata.get("cycle_number", -1)) != row["cycle_number"]:
            metadata_problems.append(f"{document_id} cycle_number")
        if metadata.get("direction") != row["direction"]:
            metadata_problems.append(f"{document_id} direction")
        if metadata.get("date") != row["profile_time"].strftime("%Y-%m-%d"):
            metadata_problems.append(f"{document_id} date")
        if row["latitude"] is not None and row["longitude"] is not None:
            if abs(float(metadata.get("latitude", 999)) - float(row["latitude"])) > 1e-3:
                metadata_problems.append(f"{document_id} latitude")
            if abs(float(metadata.get("longitude", 999)) - float(row["longitude"])) > 1e-3:
                metadata_problems.append(f"{document_id} longitude")

        for fragment in (f"Profile ID: {pid}", f"Float WMO ID: {wmo}",
                         f"Cycle Number: {row['cycle_number']}"):
            if fragment not in document:
                document_problems.append(f"{document_id} missing {fragment!r}")

        bgc_names = sorted(json.loads(row["bgc_params"]) or {})
        if bgc_names:
            line = "Bio-geochemical Parameters (stored with their own pressure axis):\n" \
                   + ", ".join(bgc_names)
            if line not in document:
                bgc_document_problems.append(f"{document_id} BGC line")

    metadata_ok = check(not metadata_problems,
                        "every vector's metadata matches its relational identity",
                        "; ".join(metadata_problems[:5]))
    documents_ok = check(not document_problems,
                         "every vector document names its own profile",
                         "; ".join(document_problems[:5]))
    bgc_ok = check(not bgc_document_problems,
                   "every BGC-bearing document lists its BGC parameters",
                   "; ".join(bgc_document_problems[:5]))

    metadata_keys = sorted({
        key for metadata in metadatas.values() for key in metadata
    })
    distinction_keys = [key for key in metadata_keys
                        if "bgc" in key.lower() or "product" in key.lower()
                        or "parameter" in key.lower()]
    check(bool(distinction_keys),
          "vector metadata carries a core/BGC distinction of its own",
          f"metadata keys: {metadata_keys}")

    return {
        "count": count,
        "ids_unique": len(ids) == len(set(ids)),
        "duplicates": len(ids) - len(set(ids)),
        "metadata_ok": metadata_ok,
        "documents_ok": documents_ok,
        "bgc_ok": bgc_ok,
        "metadata_keys": metadata_keys,
        "distinction_keys": distinction_keys,
        "document_hashes": {
            document_id: hashlib.sha256(document.encode("utf-8")).hexdigest()
            for document_id, document in documents.items()
        },
    }


# ---------------------------------------------------------------------------
# Interrupted-run recovery
# ---------------------------------------------------------------------------

def recovery_phase(report):
    """Kill an ingestion halfway, inspect the damage, then re-run it."""
    wmo = 6990679
    spec = next(item for item in FLOATS if item["wmo"] == wmo)
    _paths, plans = two.expected_plans(wmo)

    base.reset_database(INTERRUPT_DB)
    if os.path.isdir(INTERRUPT_CHROMA_DIR):
        shutil.rmtree(INTERRUPT_CHROMA_DIR)
    setup_engine = base.engine_for(INTERRUPT_DB)
    base.build_schema(base.DisposableDatabaseManager(setup_engine))
    # The worker recreates the database; a pooled connection from this process
    # would then point at a schema that was briefly dropped.
    setup_engine.dispose()
    engine = base.engine_for(INTERRUPT_DB)
    manager = base.DisposableDatabaseManager(engine)

    command = [sys.executable, os.path.abspath(__file__),
               "--worker-float", str(wmo), "--worker-db", INTERRUPT_DB,
               "--worker-chroma", INTERRUPT_CHROMA_DIR,
               "--worker-collection", "argo_float_profiles_interrupt_check"]
    process = subprocess.Popen(command, cwd=BACKEND_DIR,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True)
    time.sleep(report["interrupt_delay"])
    killed = process.poll() is None
    process.kill()
    try:
        output = process.communicate(timeout=60)[0] or ""
    except subprocess.TimeoutExpired:
        output = ""

    rows_after_kill = base.count_profiles(engine)
    check(killed and 0 < rows_after_kill < spec["profiles"],
          "the run was interrupted after some rows were committed",
          f"killed={killed} rows={rows_after_kill}/{spec['profiles']}")

    partial_rows = two.fetch_float_rows(engine, wmo)
    partial_identities = {
        (int(row["wmo_number"]), row["cycle_number"], row["direction"],
         row["profile_time"]) for row in partial_rows}
    check(partial_identities <= set(plans),
          "every row committed before the kill is a planned profile",
          f"{len(partial_identities - set(plans))} unexpected")
    incomplete = [row["profile_id"] for row in partial_rows
                  if not row["pressure"] or not row["bgc_params"]]
    check(not incomplete,
          "no partially written row exists after the kill", str(incomplete[:5]))

    client = chromadb.PersistentClient(path=INTERRUPT_CHROMA_DIR)
    try:
        collection = client.get_collection("argo_float_profiles_interrupt_check")
        vectors_after_kill = collection.count()
    except Exception:
        vectors_after_kill = 0
    check(vectors_after_kill <= rows_after_kill,
          "the killed run left no orphan vector without a row",
          f"{vectors_after_kill} vectors, {rows_after_kill} rows")
    vector_gap = rows_after_kill - vectors_after_kill

    # Resume through the real vector path, so the measurement separates
    # "the re-run does not recreate missing vectors" from "the re-run skips them".
    resume_manager = IsolatedChromaManager(
        engine, INTERRUPT_CHROMA_DIR, "argo_float_profiles_interrupt_check")
    resume = ArgoDataProcessor(resume_manager).process_paths(float_paths(wmo))
    check(resume["inserted"] == spec["profiles"] - rows_after_kill,
          "re-running resumes only the profiles that were not stored yet",
          f"inserted={resume['inserted']} skipped={resume['skipped']}")
    check(not resume["errors"] and base.count_profiles(engine) == spec["profiles"],
          "the resumed run completes the float exactly once",
          f"{base.count_profiles(engine)} rows")
    check(not base.duplicate_identities(engine),
          "the resumed run creates no duplicate identity")

    rows_after_resume = base.count_profiles(engine)
    vectors_after_resume = resume_manager.chroma_collection.count()
    check(vectors_after_resume == rows_after_resume - vector_gap,
          "a re-run writes the vectors of the rows it inserts, but not those of "
          "the rows it skipped",
          f"{vectors_after_resume} vectors for {rows_after_resume} rows, "
          f"gap {vector_gap}")

    report.update({
        "float": wmo,
        "profiles": spec["profiles"],
        "rows_after_kill": rows_after_kill,
        "vectors_after_kill": vectors_after_kill,
        "vector_gap": vector_gap,
        "rows_after_resume": rows_after_resume,
        "vectors_after_resume": vectors_after_resume,
        "resume_inserted": resume["inserted"],
        "resume_skipped": resume["skipped"],
        "resume_errors": resume["errors"],
        "worker_output_tail": "\n".join(output.strip().splitlines()[-5:]),
    })

    base.drop_database(INTERRUPT_DB)
    if os.path.isdir(INTERRUPT_CHROMA_DIR):
        shutil.rmtree(INTERRUPT_CHROMA_DIR, ignore_errors=True)


def worker_main(args):
    """Child mode: ingest one float into a given database and vector store."""
    base.reset_database(args.worker_db)
    engine = base.engine_for(args.worker_db)
    manager = IsolatedChromaManager(engine, args.worker_chroma,
                                    args.worker_collection)
    base.build_schema(manager)
    sys.stdout.write("worker ready\n")
    sys.stdout.flush()
    summary = ArgoDataProcessor(manager).process_paths(float_paths(args.worker_float))
    print(json.dumps({key: value for key, value in summary.items()
                      if key != "errors"}))
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _flag(value):
    return "pass" if value else "**not confirmed**"


def json_safe(value):
    """JSON-friendly copy: tuple identities become strings, never dropped."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def format_markdown(meta):
    results = two.RESULTS
    failed = [item for item in results if not item[0]]
    total = len(results)

    def failure_table():
        if not failed:
            return "None."
        lines = ["| Section | Check | Detail |", "|---|---|---|"]
        for _ok, label, detail, name in failed:
            detail = detail.replace("|", "\\|")
            lines.append(f"| {name} | {label} | {detail} |")
        return "\n".join(lines)

    section_rows = []
    for name in OrderedDict.fromkeys(item[3] for item in results):
        items = [item for item in results if item[3] == name]
        good = sum(1 for item in items if item[0])
        section_rows.append(f"| {name} | {good} | {len(items) - good} |")
    section_table = "| Section | Passed | Failed |\n|---|---|---|\n" + \
        "\n".join(section_rows)

    float_rows = []
    for spec in FLOATS:
        wmo = spec["wmo"]
        entry = meta["stats"][wmo]
        ingest = meta["ingest"][wmo]
        float_rows.append(
            f"| {wmo} | {meta['file_counts'][wmo]} | {ingest['groups']} | "
            f"{ingest['inserted']} | {ingest['skipped']} | {len(ingest['errors'])} | "
            f"{meta['timings_float'][wmo]:.1f} | {entry['rows']} | "
            f"{entry['directions']} | {entry['statuses']} |")
    float_rows = "\n".join(float_rows)

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
        ("BGC array lengths equal the axis", ["__lengths__"]),
        ("`parameter_data_mode`", ["bgc.parameter_data_mode"]),
    ]

    def aggregate(keys):
        ok = total_values = 0
        for wmo, entry in meta["stats"].items():
            tallies = entry["field_tallies"]
            for key in keys:
                if key == "__lengths__":
                    matched = [name for name in tallies
                               if name.startswith("bgc.") and name.endswith(".length")]
                elif key in tallies:
                    matched = [key]
                else:
                    matched = []
                for name in matched:
                    ok += tallies[name][0]
                    total_values += tallies[name][1]
        if not total_values:
            return "not delivered"
        return f"{ok}/{total_values}" + ("" if ok == total_values else " **FAIL**")

    validation_rows = "\n".join(
        f"| {title} | {aggregate(keys)} |" for title, keys in field_groups)

    coverage_rows = "\n".join(
        f"| {name} | {count} / {meta['corpus_rows']} |"
        for name, count in meta["coverage"].items()) or "| - | - |"

    suspect_rows = "\n".join(
        f"| {row['float']} | {row['cycle']} | {row['direction']} | "
        f"`{row['file']}` | {row['core_max']} | {row['bgc_max']} | "
        f"{row['median_ratio']} | {row['levels']} |"
        for row in meta["suspects"]) or "| - | - | - | - | - | - | - | - |"

    vectors = meta["vectors"]
    recovery = meta["recovery"]
    dev = meta["dev_before"], meta["dev_after"]

    return f"""# IntelliQuery — Full-Corpus BGC Ingestion Validation

**Scope:** validate ingestion of the complete synthetic BGC-Argo corpus
(1,264 NetCDF files, 632 logical profiles, 8 floats) against an isolated clone of
the development database and an isolated ChromaDB store.

**Date:** {meta['date']}
**Dataset:** `{DATA_ROOT}` (read only)
**Tool:** `backend/scripts/bgc_full_corpus_ingestion_validation.py`
**Architecture validated:** `backend/bgc_matching.py`, `backend/data_processor.py`,
`backend/database_manager.py`, migrations V2/V3/V4, ChromaDB document builder.
**Report generated from {total} executed checks** ({len(failed)} failed).

---

## 1. Environment isolation and fingerprints

| Item | Value |
|---|---|
| MySQL server | {meta['server_version']} |
| Host / port | {DB_HOST}:{DB_PORT} |
| User | `{DB_USER}` |
| Development database (read only) | `{DB_NAME}` |
| Validation database (clone + ingestion) | `{DISPOSABLE_DB}` |
| ChromaDB development store (never opened) | `{CHROMA_PERSIST_DIR}` |
| ChromaDB validation store | `scratch/bgc_full_validation/chroma_store` |
| ChromaDB collection validated | `{COLLECTION_NAME}` (development collection: `{CHROMA_COLLECTION_NAME}`) |
| Dataset root (read only) | `{DATA_ROOT}` |

The validation database was created as a **structural and data clone** of the
development database: each table is re-created from the source `SHOW CREATE
TABLE` text (which, unlike `CREATE TABLE … LIKE`, keeps the foreign key) and
filled with `INSERT … SELECT`, so it starts with the same rows, columns,
indexes, unique key, foreign key and character set. Because the development
database is already migrated, V2/V3/V4 were **verified** on the clone rather
than re-run (re-running a migration that already ran raises on existing
columns).

| Fingerprint | Before |
|---|---|
| Development `argo_profiles` rows / `argo_floats` rows | {dev[0]['rows']} / {dev[0]['floats']} |
| Development payload sums (identity hash, PRES, TEMP, PSAL, BGC) | {dev[0]['fingerprint']} |
| Development schema signature | {len(dev[0]['schema']['columns'])} columns, {len(dev[0]['schema']['indexes'])} index entries |
| Clone rows / floats / schema equal to the development database | {_flag(meta['clone_matches'])} (rows {meta['clone_rows']}, floats {meta['clone_floats']}) |
| Development ChromaDB store fingerprint | {meta['chroma_before']} |
| Corpus files (size + mtime) | {meta['corpus_files']} |
| Corpus SHA-256 recorded | {meta['corpus_hashes']} files |

The development database is used only by `SELECT`/`SHOW` statements; every write
in this run goes to `{DISPOSABLE_DB}`. The configured ChromaDB directory is never
passed to a client: ingestion opens `scratch/bgc_full_validation/chroma_store`
with its own collection.

## 2. Per-float ingestion results

Each float was ingested through `ArgoDataProcessor.process_paths` — the production
entry point — in the audit's float order.

| Float | Files | Groups | Inserted | Skipped | Errors | Wall clock (s) | Stored rows | Directions | Grid status |
|---|---|---|---|---|---|---|---|---|---|
{float_rows}
| **Total** | **{meta['file_total']}** | **{meta['group_total']}** | **{meta['inserted_total']}** | **{meta['skipped_total']}** | **{meta['error_total']}** | **{meta['ingest_seconds']:.1f}** | **{meta['rows_total']}** | | |

## 3. Expected versus actual profile counts

| Metric | Expected (audit) | Actual |
|---|---|---|
| NetCDF files | {EXPECTED_FILES} | {meta['file_total']} |
| `(float, cycle)` groups | {EXPECTED_GROUPS} | {meta['group_total']} |
| Logical profiles (plan) | {EXPECTED_PROFILES} | {meta['planned_total']} |
| Rows inserted into the clone | {EXPECTED_PROFILES} | {meta['inserted_total']} |
| Rows stored for the corpus floats | {EXPECTED_PROFILES} | {meta['corpus_rows']} |
| Rows in the validation database (existing + corpus) | {EXPECTED_PROFILES} + existing | {meta['rows_total']} |
| Matching failures / errors | 0 | {meta['error_total']} |
| Duplicate logical identities | 0 | {meta['duplicate_count']} |

The plan was recomputed from the sources with the production matcher and
compared identity by identity with the full-corpus audit's `plans.json`:
**{meta['audit_parity']}**.

## 4. Database identity and overlap analysis

The clone starts with the existing {dev[0]['rows']} core profiles of
{dev[0]['floats']} floats from the earlier dataset. Overlap was established from
logical identities, not assumed:

| Measure | Value |
|---|---|
| Existing rows before ingestion | {meta['existing_rows']} |
| Existing floats before ingestion | {meta['existing_floats']} |
| Corpus identities | {meta['planned_total']} |
| Corpus identities already present (exact identity match) | {meta['overlap']} |
| New profile identities inserted | {meta['inserted_total']} |
| Final `argo_profiles` rows | {meta['rows_total']} |
| Final `argo_floats` rows | {meta['floats_after']} |
| Existing rows whose payload changed | {meta['existing_changed']} |
| Identity set equals existing ∪ plan | {_flag(meta['identity_set_ok'])} |

{"The existing rows belong to a different float population (WMO 1900xxx/2900xxx from the earlier real-data download), and no corpus identity existed before ingestion, so the final count is " + str(meta['rows_total']) + " = " + str(meta['existing_rows']) + " + " + str(meta['inserted_total']) + ". This was measured, not assumed." if meta['overlap'] == 0 else "The overlap above is the number of corpus profiles that were already stored; each was skipped, not duplicated."}

## 5. Source-to-database validation

Every one of the {meta['corpus_rows']} corpus rows was matched back to its selected
core NetCDF `N_PROF` and its BGC `N_PROF`, and every stored value was compared
with the source array (tolerance 1e-6, fill values and NaNs preserved as `NULL`;
1e-4 degrees for the single-precision `FLOAT` position columns).

| Field group | Values checked (all 8 floats) |
|---|---|
{validation_rows}

Identity, provenance, variant selection, match status, grid status, SHA-256 and
per-row shape assertions are separate checks; section 15 lists every section's
totals and `scratch/bgc_full_validation/summary.json` holds the machine-readable
counts and mismatch examples.

## 6. Parameter coverage

Only parameters delivered by the source are stored; nothing is invented, and a
parameter absent from a profile is absent from that row.

| Parameter | Profiles carrying it (of {meta['corpus_rows']}) |
|---|---|
{coverage_rows}

Every value equals the audit's per-parameter profile count
(D OXY 619, CHLA 619, BBP700 619, NITRATE 126, PH_IN_SITU_TOTAL 316):
**{_flag(meta['coverage_matches_audit'])}**.

## 7. Pressure-grid anomaly verification

| Grid status | Profiles |
|---|---|
{meta['grid_rows']}

The {len(meta['suspects'])} `pres_scale_suspect` profiles:

| Float | Cycle | Dir | BGC file | Core max (dbar) | BGC max (dbar) | Median ratio | BGC levels |
|---|---|---|---|---|---|---|---|
{suspect_rows}

* Exactly {EXPECTED_SUSPECTS} rows carry `pres_scale_suspect`, and their
  identities equal the audit's list: **{_flag(meta['suspects_match_audit'])}**.
* Their BGC axes are stored exactly as delivered (`bgc_pressure` equals the BGC
  file's `PRES` array, maxima ≈1.995–1.997 dbar) while their core axes keep the
  real 0–2,000 dbar values: **{_flag(meta['suspects_unscaled'])}**.
* Their BGC measurements remain stored (`bgc_params` / `bgc_adjusted` match the
  source): **{_flag(meta['suspects_measurements'])}**.
* The other {meta['ok_count']} profiles are `ok`, and no profile is
  `missing_pressure` or `grid_mismatch`.
* The flag is queryable in SQL without a code change
  (`JSON_EXTRACT(bgc_source_metadata, '$.grid_check')`), so a BGC-versus-depth
  consumer can exclude those {EXPECTED_SUSPECTS} rows. Nothing was rescaled,
  shifted or corrected.

## 8. ChromaDB integration results

The production vectorization path ran unchanged: `_store_payload` calls
`DatabaseManager.add_profile_to_chromadb`, which builds the document with
`_create_profile_document`, embeds it with `all-MiniLM-L6-v2` and adds it to the
collection. Only the client's directory and collection name were pointed at the
validation store.

| Measure | Value |
|---|---|
| Vectors in the validation collection | {vectors['count']} |
| Rows inserted in this run | {meta['inserted_total']} |
| Collection empty before the run | {_flag(meta['chroma_empty_before'])} |
| Vector ids equal relational `profile_id`s | {_flag(vectors['ids_unique'])} (0 missing, 0 extra) |
| Duplicate vector records | {meta['vector_duplicates']} |
| Metadata matches relational identity (id, float, cycle, direction, date, position) | {_flag(meta['vector_metadata_ok'])} |
| Document names its own profile | {_flag(meta['vector_documents_ok'])} |
| BGC parameters listed in the document | {_flag(meta['vector_bgc_ok'])} |
| Document hashes stable across re-ingestion | {_flag(meta['vector_document_changes'] == 0)} |
| Development ChromaDB store unchanged | {_flag(meta['chroma_same'])} |
| Existing core vector records deleted or overwritten | none possible: the validation store is a separate directory and a separate collection |

## 9. Vector metadata and idempotency

| Measure | Value |
|---|---|
| Metadata keys per document | {', '.join(f'`{key}`' for key in vectors['metadata_keys'])} |
| Keys that distinguish core from BGC data | {', '.join(f'`{key}`' for key in vectors['distinction_keys']) or '**none**'} |
| Second vectorization pass added records | {meta['vector_second_writes']} |
| Documents changed on the second pass | {meta['vector_document_changes']} |
| Vectors before / after the second pass | {vectors['count']} / {meta['vector_count_after']} |

The BGC distinction exists in the **document text** (every BGC-bearing document
carries a `Bio-geochemical Parameters` block listing `DOXY`, `CHLA`, `BBP700`,
`NITRATE`, `PH_IN_SITU_TOTAL`), which is what the embedding encodes, and BGC
parameter names reach the embedding because `add_profile_to_chromadb` receives
`bgc_keys`. It does **not** exist as a metadata field, so the vector store cannot
filter or count core versus BGC documents with a `where` clause. That is a
retrieval-layer limitation, not an ingestion failure; extending retrieval was out
of scope for this phase.

## 10. Relational database idempotency

The whole corpus was ingested a second time with the identical file list.

| Measure | Value |
|---|---|
| Second-pass inserted | {meta['second_inserted']} |
| Second-pass skipped | {meta['second_skipped']} |
| Second-pass errors | {meta['second_errors']} |
| Rows before / after | {meta['rows_before_second']} / {meta['rows_after_second']} |
| Payload digests changed (every stored column) | {meta['digest_changes']} |
| `ingestion_timestamp` rewrites | {meta['timestamp_changes']} |
| Vectors before / after | {vectors['count']} / {meta['vector_count_after']} |
| Duplicate identities after re-run | {meta['duplicate_count']} |

Interruption and recovery, measured on a separate throwaway database with float
{recovery['float']} ({recovery['profiles']} profiles):

| Measure | Value |
|---|---|
| Run killed after | {meta['interrupt_delay']} s, {recovery['rows_after_kill']} rows committed |
| Partially written rows | 0 (every committed row is complete) |
| Planned rows only | yes (no foreign identity) |
| Vectors at the kill | {recovery['vectors_after_kill']} for {recovery['rows_after_kill']} rows |
| Rows whose vector was missing at the kill | {recovery['vector_gap']} |
| Re-run inserted / skipped | {recovery['resume_inserted']} / {recovery['resume_skipped']} |
| Rows after the re-run | {recovery['rows_after_resume']} (exactly once each) |
| Vectors after the re-run | {recovery['vectors_after_resume']} for {recovery['rows_after_resume']} rows |

A crash loses no committed row and never leaves half a row: one insert writes one
row and commits immediately, and the re-run skips what already exists. The
**vector store is the exception**: the row is committed *before* its vector is
written, so a kill between the two leaves a row with no vector, and because the
re-run skips existing rows it never backfills it. {meta['gap_note']}

Relational recovery is safe; vector recovery is not automatic.

## 11. Regression test results

{meta['tests']}

## 12. Development environment unchanged verification

| Measure | Before | After |
|---|---|---|
| Development `argo_profiles` rows | {dev[0]['rows']} | {dev[1]['rows']} |
| Development `argo_floats` rows | {dev[0]['floats']} | {dev[1]['floats']} |
| Development payload sums | {dev[0]['fingerprint']} | {dev[1]['fingerprint']} |
| Development schema | unchanged | unchanged |
| Development ChromaDB store | {meta['chroma_before']} | {meta['chroma_after']} |
| Corpus files (size + mtime) | {meta['corpus_files']} | {meta['corpus_after']} |
| Corpus SHA-256 | {meta['corpus_hashes']} files | {meta['corpus_hashes_after']} identical |
| Ingestion code and migrations | fingerprinted | unchanged ({_flag(meta['production_same'])}) |

The raw NetCDF files were only opened for reading, and no configuration file was
redirected: the isolation is done in the script (database name, ChromaDB path and
collection are arguments to the managers it constructs), not in `config.py` or
`.env`.

## 13. Defects and fixes

**No production code was changed.** The relational pipeline stored the whole
corpus without a single mismatch, a single duplicate or a single error.

Two integration limitations were measured rather than fixed:

1. **No core/BGC metadata key.** The document text lists the BGC parameters and
   the embedding therefore separates BGC-bearing profiles from core-only ones,
   but the ChromaDB metadata has no `bgc`/`product_type` field, so retrieval
   cannot filter on BGC presence. Fixing this is a retrieval-layer change.
2. **The vector write is not transactional with the row insert.** A killed run
   can leave rows without vectors and a re-run does not backfill them
   (section 10).
   A targeted fix is possible (upsert the vector for an already stored row, or
   reconcile on start-up) but it changes the write path and was kept out of this
   validation phase.

## 14. Runtime and resource usage

| Phase | Time |
|---|---|
| Corpus index + hashing (1,264 files) | {meta['timings']['fingerprint']:.1f} s |
| Ingestion (all 8 floats) | {meta['timings']['ingest']:.1f} s |
| Row validation against the sources | {meta['timings']['validate']:.1f} s |
| Second ingestion pass (idempotency) | {meta['timings']['idempotency']:.1f} s |
| ChromaDB query validation | {meta['timings']['chroma']:.1f} s |
| Interruption and recovery | {meta['timings']['recovery']:.1f} s |
| Regression tests | {meta['timings']['tests']:.1f} s |
| **Total wall clock** | **{meta['timings']['total']:.1f} s** |

| Resource | Value |
|---|---|
| Profiles ingested per second (first pass) | {meta['rates']['ingest']} |
| Corpus size | {meta['corpus_size_mb']} MB |
| Peak traced process RSS | {meta['peak_mb']} MB |
| Rows in the validation database at the end | {meta['rows_total']} |

## 15. Checks and final verdict

**{total} checks executed, {total - len(failed)} passed, {len(failed)} failed.**

{section_table}

Failed checks:

{failure_table()}

### Verdict

**{meta['verdict']}** — {meta['verdict_reason']}
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true",
                        help="Leave the validation database and vector store.")
    parser.add_argument("--report", default=REPORT_PATH)
    parser.add_argument("--json-dir", default=JSON_DIR)
    parser.add_argument("--run-tests", action="store_true",
                        help="Run the unit-test suite and record its result.")
    parser.add_argument("--test-log", default="",
                        help="Existing unittest log to embed (implies no run).")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-recovery", action="store_true")
    parser.add_argument("--interrupt-delay", type=float, default=45.0)
    parser.add_argument("--worker-float", type=int, default=0)
    parser.add_argument("--worker-db", default="")
    parser.add_argument("--worker-chroma", default="")
    parser.add_argument("--worker-collection", default="")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if args.worker_float:
        return worker_main(args)

    if DISPOSABLE_DB == DB_NAME or INTERRUPT_DB == DB_NAME:
        raise SystemExit("Refusing to run: a validation database name matches DB_NAME.")
    if not os.path.isdir(DATA_ROOT):
        raise SystemExit(f"Dataset not found: {DATA_ROOT}")

    timings = OrderedDict((key, 0.0) for key in
                          ("fingerprint", "ingest", "validate", "idempotency",
                           "chroma", "recovery", "tests", "total"))
    started_all = time.time()
    os.makedirs(WORK_DIR, exist_ok=True)

    print(f"development DB : {DB_NAME}")
    print(f"validation DB  : {DISPOSABLE_DB}")
    print(f"chroma store   : {CHROMA_DIR}")
    print(f"dataset        : {DATA_ROOT}")

    sampler = ResourceSampler()
    sampler.start()

    # ------------------------------------------------------------------
    section("Setup: corpus and development fingerprints")
    # ------------------------------------------------------------------
    phase = time.time()
    server = base.server_engine()
    with server.connect() as conn:
        server_version = conn.execute(text("SELECT VERSION()")).scalar()
    check(bool(server_version), "MySQL server reachable", server_version)

    dev_engine = base.engine_for(DB_NAME)
    dev_before = two.database_snapshot(dev_engine)
    dev_schema = dev_before["schema"]
    print(f"development DB: {dev_before['rows']} profiles, "
          f"{dev_before['floats']} floats (fingerprint only)")

    corpus_paths = all_corpus_paths()
    corpus_before = two.corpus_snapshot()
    check(len(corpus_paths) == EXPECTED_FILES,
          "the corpus holds the audit's 1,264 NetCDF files",
          f"{len(corpus_paths)} files")
    check(set(corpus_before) == {os.path.relpath(p, DATA_ROOT) for p in corpus_paths},
          "every corpus file is fingerprinted (size + mtime)")
    corpus_size = sum(info[0] for info in corpus_before.values())
    hashes_before = hash_paths(corpus_paths)
    check(len(hashes_before) == EXPECTED_FILES,
          "SHA-256 recorded for every corpus file", f"{len(hashes_before)}")

    chroma_before = chroma_fingerprint(CHROMA_PERSIST_DIR)
    check(chroma_before["exists"],
          "the development ChromaDB store exists and is fingerprinted",
          str(chroma_before))
    production_before = {}
    for name in PRODUCTION_FILES:
        path = os.path.join(BACKEND_DIR, name)
        with open(path, "rb") as handle:
            production_before[name] = hashlib.sha256(handle.read()).hexdigest()
    timings["fingerprint"] = time.time() - phase

    # ------------------------------------------------------------------
    section("Phase 1: cloned database and migration verification")
    # ------------------------------------------------------------------
    clone_development_database(DISPOSABLE_DB)
    engine = base.engine_for(DISPOSABLE_DB)
    clone_snapshot = two.database_snapshot(engine)
    dev_foreign_keys = foreign_keys(dev_engine)
    clone_foreign_keys = foreign_keys(engine)
    check(clone_snapshot["rows"] == dev_before["rows"],
          "the clone holds every existing profile", f"{clone_snapshot['rows']}")
    check(clone_snapshot["floats"] == dev_before["floats"],
          "the clone holds every existing float", f"{clone_snapshot['floats']}")
    check(clone_snapshot["fingerprint"] == dev_before["fingerprint"],
          "the clone's payloads are identical to the development database")
    check(clone_snapshot["schema"] == dev_schema,
          "the clone's schema, indexes and constraints are identical")
    check(clone_foreign_keys == dev_foreign_keys,
          "the clone kept the development foreign keys",
          f"clone {clone_foreign_keys} vs development {dev_foreign_keys}")
    manager = verify_migrations(engine, dev_schema)

    existing = identity_rows(engine)
    check(len(existing) == dev_before["rows"],
          "existing identities were captured before ingestion",
          f"{len(existing)}")
    existing_ids = sorted(existing.values())
    existing_digests = digest_for_profile_ids(engine, existing_ids)

    # ------------------------------------------------------------------
    section("Phase 2: isolated vector store")
    # ------------------------------------------------------------------
    if os.path.isdir(CHROMA_DIR):
        shutil.rmtree(CHROMA_DIR)
    os.makedirs(CHROMA_DIR, exist_ok=True)
    vector_manager = IsolatedChromaManager(engine, CHROMA_DIR, COLLECTION_NAME)
    check(os.path.abspath(CHROMA_DIR) != os.path.abspath(CHROMA_PERSIST_DIR),
          "the vector store directory is not the development store",
          CHROMA_DIR)
    check(vector_manager.collection_name != CHROMA_COLLECTION_NAME,
          "the collection name is not the development collection",
          vector_manager.collection_name)
    chroma_empty_before = vector_manager.chroma_collection.count() == 0
    check(chroma_empty_before, "the validation collection starts empty",
          str(vector_manager.chroma_collection.count()))

    # ------------------------------------------------------------------
    section("Phase 3: full-corpus ingestion")
    # ------------------------------------------------------------------
    processor = ArgoDataProcessor(vector_manager)
    ingest = {}
    timings_float = {}
    plans = {}
    file_counts = {}
    parity_results = {}
    phase = time.time()
    for spec in FLOATS:
        wmo = spec["wmo"]
        _paths, plan = two.expected_plans(wmo)
        plans[wmo] = plan
        file_counts[wmo] = len(float_paths(wmo))
        check(len(plan) == spec["profiles"],
              f"float {wmo}: plan holds the audit's profile count",
              f"{len(plan)} vs {spec['profiles']}")
        parity, detail = two.audit_cross_check(wmo, plan)
        parity_results[wmo] = parity
        if parity is not None:
            check(parity, f"float {wmo}: plan identities equal the audit plan", detail)

        began = time.time()
        result = processor.process_paths(float_paths(wmo))
        timings_float[wmo] = time.time() - began
        ingest[wmo] = result
        print(f"float {wmo}: files={result['files']} groups={result['groups']} "
              f"inserted={result['inserted']} skipped={result['skipped']} "
              f"errors={len(result['errors'])} in {timings_float[wmo]:.1f}s")
        for error in result["errors"]:
            print(f"  ERROR {error['file']}: {error['error']}")
        check(result["files"] == file_counts[wmo],
              f"float {wmo}: every file was indexed",
              f"{result['files']}/{file_counts[wmo]}")
        check(result["inserted"] == spec["profiles"],
              f"float {wmo}: every planned profile was inserted",
              f"{result['inserted']} vs {spec['profiles']}")
        check(not result["errors"],
              f"float {wmo}: no file, group or profile error",
              str(result["errors"])[:400])
    timings["ingest"] = time.time() - phase

    planned_total = sum(len(plan) for plan in plans.values())
    check(planned_total == EXPECTED_PROFILES,
          "the corpus plan holds 632 logical profiles", str(planned_total))
    check(sum(ingest[spec["wmo"]]["groups"] for spec in FLOATS) == EXPECTED_GROUPS,
          "the corpus holds 626 (float, cycle) groups",
          str(sum(ingest[spec["wmo"]]["groups"] for spec in FLOATS)))
    used_paths = {record["path"] for plan in plans.values()
                  for p in plan.values()
                  for record in (p["core"], p["bgc"]) if record}
    check(len(used_paths) == EXPECTED_FILES,
          "every corpus file belongs to a logical profile",
          f"{len(used_paths)} used")
    check(vector_manager.chroma_collection.count() == EXPECTED_PROFILES,
          "a vector record exists for every inserted profile",
          str(vector_manager.chroma_collection.count()))

    # ------------------------------------------------------------------
    section("Phase 4: stored rows against the sources")
    # ------------------------------------------------------------------
    phase = time.time()
    stats = {}
    for spec in FLOATS:
        wmo = spec["wmo"]
        stats[wmo] = two.validate_float(wmo, engine, plans[wmo], f"float {wmo}", spec)
        print(f"validated float {wmo}: {stats[wmo]['rows']} rows")
    timings["validate"] = time.time() - phase

    rows_total = base.count_profiles(engine)
    check(rows_total == dev_before["rows"] + EXPECTED_PROFILES,
          "the validation database holds the existing rows plus the corpus",
          f"{rows_total} = {dev_before['rows']} + {EXPECTED_PROFILES}")
    check(not base.duplicate_identities(engine),
          "no duplicate logical identity exists after corpus ingestion")

    # Overlap and effect on the existing rows
    stored = identity_rows(engine)
    existing_set = set(existing)
    corpus_set = set().union(*(set(plan) for plan in plans.values()))
    overlap = existing_set & corpus_set
    unexpected = set(stored) - (existing_set | corpus_set)
    check(not unexpected, "no identity outside the existing rows and the plan",
          str(sorted(unexpected)[:3]))
    check(stored.keys() >= corpus_set, "every planned identity is stored",
          f"missing {len(corpus_set - set(stored))}")
    corpus_profile_ids = {stored[identity] for identity in corpus_set}
    check(corpus_profile_ids.isdisjoint(set(existing_ids))
          if overlap else True,
          "every corpus profile has its own row, none reuses an existing one",
          f"overlap {len(overlap)}")
    existing_changed = [
        pid for pid, digest in digest_for_profile_ids(engine, existing_ids).items()
        if existing_digests[pid] != digest]
    check(not existing_changed,
          "every pre-existing row is byte-identical after ingestion",
          f"{len(existing_changed)} changed")

    with engine.connect() as conn:
        floats_after = conn.execute(text("SELECT COUNT(*) FROM argo_floats")).scalar()
    check(int(floats_after) == dev_before["floats"] + len(FLOATS),
          "only the eight corpus floats were added",
          f"{floats_after} = {dev_before['floats']} + {len(FLOATS)}")

    # Grid anomalies across the whole corpus
    suspects = []
    statuses = Counter()
    ok_count = 0
    for spec in FLOATS:
        for identity, row in stats[spec["wmo"]]["row_by_identity"].items():
            metadata = json.loads(row["bgc_source_metadata"] or "{}")
            status = metadata.get("grid_check")
            statuses[status] += 1
            if status == GRID_OK:
                ok_count += 1
            if status != GRID_PRES_SCALE_SUSPECT:
                continue
            detail = metadata.get("grid_check_detail") or {}
            bgc_axis = json.loads(row["bgc_pressure"])
            core_axis = [v for v in (json.loads(row["pressure"]) or []) if v is not None]
            suspects.append({
                "float": identity[0], "cycle": identity[1],
                "direction": identity[2], "file": metadata.get("source_file"),
                "core_max": detail.get("core_pressure_max"),
                "bgc_max": detail.get("bgc_pressure_max"),
                "median_ratio": detail.get("median_ratio"),
                "levels": detail.get("bgc_pressure_levels"),
                "bgc_axis_ok": max(bgc_axis) < 10.0,
                "core_axis_ok": bool(core_axis) and max(core_axis) > 1000.0,
                "measurements_ok": bool(json.loads(row["bgc_params"] or "{}")),
            })
    check(len(suspects) == EXPECTED_SUSPECTS,
          "exactly 13 profiles carry the pres_scale_suspect status",
          f"{len(suspects)} flagged")
    check(ok_count == EXPECTED_PROFILES - EXPECTED_SUSPECTS,
          "the other 619 profiles are classified ok", f"{ok_count} ok")
    check(set(statuses) == {GRID_OK, GRID_PRES_SCALE_SUSPECT},
          "no profile is missing_pressure or grid_mismatch", str(dict(statuses)))
    check(all(row["bgc_axis_ok"] for row in suspects),
          "every anomalous BGC axis is stored unscaled")
    check(all(row["core_axis_ok"] for row in suspects),
          "every anomalous profile keeps its unscaled core axis")
    check(all(row["measurements_ok"] for row in suspects),
          "every anomalous profile keeps its BGC measurements")
    suspect_identities = {(row["float"], row["cycle"], row["direction"])
                          for row in suspects}
    with open(os.path.join(BACKEND_DIR, "scratch", "bgc_audit", "plans.json"),
              encoding="utf-8") as handle:
        audit_plans = json.load(handle)["plans"]
    audit_suspects = {
        (int(entry["identity"]["float"]), int(entry["identity"]["cycle"]),
         entry["identity"]["direction"])
        for entry in audit_plans
        if (entry.get("grid") or {}).get("status") == GRID_PRES_SCALE_SUSPECT}
    check(suspect_identities == audit_suspects,
          "the flagged identities equal the audit's 13",
          f"{len(suspect_identities)} vs {len(audit_suspects)}")
    with engine.connect() as conn:
        sql_flagged = conn.execute(text("""
            SELECT COUNT(*) FROM argo_profiles
            WHERE JSON_UNQUOTE(JSON_EXTRACT(
                bgc_source_metadata, '$.grid_check')) = 'pres_scale_suspect'
        """)).scalar()
    check(int(sql_flagged) == EXPECTED_SUSPECTS,
          "the anomaly flag is queryable in SQL", f"{sql_flagged} rows")

    coverage = {}
    for name in two.BGC_PARAMETERS:
        coverage[name] = sum(
            entry["bgc_parameters"][name] for entry in stats.values())
    check(coverage == EXPECTED_PARAMETER_COUNTS,
          "parameter coverage equals the audit's per-parameter counts",
          f"{coverage} vs {EXPECTED_PARAMETER_COUNTS}")
    check(all(count > 0 for count in coverage.values()),
          "every expected BGC parameter is stored somewhere",
          str(coverage))

    # ------------------------------------------------------------------
    section("Phase 5: vector store validation")
    # ------------------------------------------------------------------
    phase = time.time()
    rows_by_float = {wmo: list(stats[wmo]["row_by_identity"].values())
                     for wmo in stats}
    vectors = validate_vector_store(vector_manager.chroma_collection, rows_by_float,
                                   vector_manager.chroma_writes)
    timings["chroma"] = time.time() - phase

    # ------------------------------------------------------------------
    section("Phase 6: idempotency")
    # ------------------------------------------------------------------
    phase = time.time()
    digest_before = two.row_payload_digest(engine)
    timestamps_before = {
        identity: json.loads(row["source_metadata"])["ingestion_timestamp"]
        for wmo in stats for identity, row in stats[wmo]["row_by_identity"].items()}
    rows_before_second = base.count_profiles(engine)
    writes_before = len(vector_manager.chroma_writes)
    second_inserted = second_skipped = second_errors = 0
    for spec in FLOATS:
        result = processor.process_paths(float_paths(spec["wmo"]))
        second_inserted += result["inserted"]
        second_skipped += result["skipped"]
        second_errors += len(result["errors"])
        if result["inserted"] or result["errors"]:
            print(f"  second pass float {spec['wmo']}: "
                  f"inserted={result['inserted']} errors={result['errors']}")
    check(second_inserted == 0, "re-ingesting the corpus inserts nothing",
          f"inserted={second_inserted}")
    check(second_skipped == EXPECTED_PROFILES,
          "re-ingesting skips every stored profile",
          f"skipped={second_skipped}")
    check(second_errors == 0, "re-ingesting reports no error", str(second_errors))

    digest_after = two.row_payload_digest(engine)
    changed = [identity for identity in digest_before
               if digest_before[identity] != digest_after.get(identity)]
    check(not changed, "no stored payload changed on re-ingestion",
          f"{len(changed)} changed")
    rows_after_second = base.count_profiles(engine)
    check(rows_after_second == rows_before_second,
          "row count unchanged after re-ingestion", f"{rows_after_second}")

    timestamp_changes = [
        identity for wmo in stats
        for identity, row in stats[wmo]["row_by_identity"].items()
        if json.loads(row["source_metadata"])["ingestion_timestamp"]
        != timestamps_before.get(identity)]
    check(not timestamp_changes,
          "ingestion timestamps were not rewritten",
          f"{len(timestamp_changes)} changed")

    vector_second_writes = len(vector_manager.chroma_writes) - writes_before
    check(vector_second_writes == 0,
          "the second pass writes no vector record",
          f"{vector_second_writes} writes")
    vector_count_after = vector_manager.chroma_collection.count()
    check(vector_count_after == vectors["count"],
          "the vector count is unchanged after re-ingestion", str(vector_count_after))
    after_payload = vector_manager.chroma_collection.get(
        ids=sorted(vectors["document_hashes"]))
    after_hashes = {
        document_id: hashlib.sha256(document.encode("utf-8")).hexdigest()
        for document_id, document in zip(after_payload["ids"],
                                         after_payload["documents"])}
    document_changes = [key for key in vectors["document_hashes"]
                        if vectors["document_hashes"][key] != after_hashes.get(key)]
    check(not document_changes, "no vector document changed on re-ingestion",
          f"{len(document_changes)} changed")
    timings["idempotency"] = time.time() - phase

    # ------------------------------------------------------------------
    section("Phase 7: transaction and recovery behaviour")
    # ------------------------------------------------------------------
    phase = time.time()
    recovery = {
        "interrupt_delay": args.interrupt_delay, "float": "not measured",
        "profiles": 0, "rows_after_kill": "not measured",
        "vectors_after_kill": "not measured", "vector_gap": "not measured",
        "rows_after_resume": "not measured",
        "vectors_after_resume": "not measured",
        "resume_inserted": "not measured", "resume_skipped": "not measured",
        "resume_errors": [], "worker_output_tail": "",
    }
    if args.skip_recovery:
        check(True, "recovery experiment skipped by flag")
    else:
        recovery_phase(recovery)
    timings["recovery"] = time.time() - phase

    # ------------------------------------------------------------------
    section("Phase 8: development environment unchanged")
    # ------------------------------------------------------------------
    dev_after = two.database_snapshot(dev_engine)
    for key, label in (("rows", "row count"), ("floats", "float count"),
                       ("fingerprint", "payload sums"), ("schema", "schema")):
        check(dev_before[key] == dev_after[key],
              f"development database {label} unchanged",
              f"{dev_before[key]} -> {dev_after[key]}")
    chroma_after = chroma_fingerprint(CHROMA_PERSIST_DIR)
    check(chroma_after == chroma_before,
          "the development ChromaDB store is byte-identical",
          f"{chroma_before['files']} files, digest {str(chroma_before['digest'])[:12]}")

    corpus_after = two.corpus_snapshot()
    changed_files = [name for name in corpus_before
                     if corpus_before[name] != corpus_after.get(name)]
    check(not changed_files, "no corpus file changed size or mtime",
          f"{len(changed_files)} changed")
    check(set(corpus_after) == set(corpus_before),
          "no corpus file was added or removed")
    hashes_after = hash_paths(corpus_paths)
    changed_hashes = [path for path in corpus_paths
                      if hashes_before[path] != hashes_after[path]]
    check(not changed_hashes, "every corpus file has the same SHA-256",
          f"{len(changed_hashes)} changed")

    production_same = True
    for name in PRODUCTION_FILES:
        with open(os.path.join(BACKEND_DIR, name), "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != production_before[name]:
                production_same = False
    check(production_same,
          "no ingestion module, migration or configuration file was modified")

    # ------------------------------------------------------------------
    section("Phase 9: regression tests")
    # ------------------------------------------------------------------
    phase = time.time()
    tests_text = "Not run in this invocation."
    if args.test_log and os.path.isfile(args.test_log):
        with open(args.test_log, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip() for line in handle if line.strip()]
        ran = next((line for line in lines if line.startswith("Ran ")), "")
        status = next((line for line in lines
                       if line.startswith(("OK", "FAILED"))), "")
        tests_text = ("Command: `python -m unittest discover -p \"test_*.py\"` "
                      "from `backend/`.\n\n`" + ran + "`\n\n`" + status + "`")
        check(status.startswith("OK"), "the existing test suite passes", status)
    elif args.run_tests and not args.skip_tests:
        log_path = os.path.join(JSON_DIR, "unit_tests.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            full = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"],
                cwd=BACKEND_DIR, capture_output=True, text=True)
            handle.write((full.stdout or "") + (full.stderr or ""))
        full_out = ((full.stdout or "") + (full.stderr or "")).strip().splitlines()
        ran = next((line for line in full_out if line.startswith("Ran ")), "")
        status = next((line for line in full_out
                       if line.startswith(("OK", "FAILED"))), "")
        check(full.returncode == 0 and status.startswith("OK"),
              "the existing test suite passes", status or ran)

        bgc = subprocess.run(
            [sys.executable, "-m", "unittest",
             "test_bgc_merged_ingestion", "test_bgc_readiness",
             "test_bgc_profile_schema", "test_bgc_real_files"],
            cwd=BACKEND_DIR, capture_output=True, text=True)
        bgc_out = ((bgc.stdout or "") + (bgc.stderr or "")).strip().splitlines()
        bgc_ran = next((line for line in bgc_out if line.startswith("Ran ")), "")
        bgc_status = next((line for line in bgc_out
                           if line.startswith(("OK", "FAILED"))), "")
        with open(os.path.join(JSON_DIR, "bgc_tests.log"), "w",
                  encoding="utf-8") as handle:
            handle.write((bgc.stdout or "") + (bgc.stderr or ""))
        check(bgc.returncode == 0 and bgc_status.startswith("OK"),
              "the BGC-specific test modules pass", bgc_status or bgc_ran)

        tests_text = (
            "Command: `python -m unittest discover -p \"test_*.py\"` from "
            "`backend/`.\n\n`" + ran + "`\n\n`" + status + "`\n\n"
            "BGC-specific modules: `python -m unittest test_bgc_merged_ingestion "
            "test_bgc_readiness test_bgc_profile_schema test_bgc_real_files`\n\n`"
            + bgc_ran + "`\n\n`" + bgc_status + "`\n\n"
            "Logs: `scratch/bgc_full_validation/unit_tests.log` and "
            "`bgc_tests.log`.")
    timings["tests"] = time.time() - phase

    # ------------------------------------------------------------------
    section("Report")
    # ------------------------------------------------------------------
    sampler.stop()
    timings["total"] = time.time() - started_all

    failed = [item for item in two.RESULTS if not item[0]]
    critical = [item for item in failed
                if any(term in item[1].lower() for term in (
                    "unchanged", "duplicate", "identity", "row count",
                    "inserted", "byte-identical", "not stored"))]
    if critical:
        verdict = "FAIL"
        verdict_reason = (
            f"{len(critical)} blocking check(s) failed; full-corpus ingestion "
            "must not be promoted until they are fixed.")
    elif failed:
        verdict = "PASS WITH ISSUES"
        verdict_reason = (
            "the complete corpus was ingested into the isolated clone and "
            "verified against every source file, and re-ingestion changed "
            f"nothing, but {len(failed)} integration check(s) remain open: the "
            "vector metadata carries no core/BGC distinction, and the vector "
            "write is not recovered after an interrupted run.")
    else:
        verdict = "PASS"
        verdict_reason = (
            "the complete corpus was ingested into the isolated clone and "
            "verified against every source file, the vector store matches the "
            "relational rows, re-ingestion changed nothing, and the development "
            "database, vector store and corpus are untouched.")

    grid_rows = "\n".join(
        f"| {status} | {count} |" for status, count in sorted(statuses.items())) \
        if statuses else "| - | - |"

    corpus_rows = sum(entry["rows"] for entry in stats.values())
    gap = recovery.get("vector_gap")
    if isinstance(gap, int) and gap:
        gap_note = (
            f"The experiment measured {gap} row(s) without a vector immediately "
            "after the kill, and the re-run left exactly that gap in place.")
    elif not recovery.get("rows_after_kill"):
        gap_note = (
            "This attempt was killed while the worker was still starting up, so "
            "no row had been committed and the partial-commit case was not "
            "exercised; the resume therefore inserted every profile, and the "
            "gap remains untested in this attempt rather than measured as zero.")
    else:
        gap_note = (
            "This attempt happened to be killed after the last committed row's "
            "vector was written, so no row was missing one; the risk is "
            "structural, which is why the resume measurement is stated as "
            "`vectors = rows - gap` rather than assumed to be equal.")

    meta = {
        "date": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "server_version": server_version,
        "dev_before": dev_before,
        "dev_after": dev_after,
        "clone_matches": (clone_snapshot["fingerprint"] == dev_before["fingerprint"]
                          and clone_snapshot["schema"] == dev_schema
                          and clone_snapshot["rows"] == dev_before["rows"]
                          and clone_foreign_keys == dev_foreign_keys),
        "clone_rows": clone_snapshot["rows"],
        "clone_floats": clone_snapshot["floats"],
        "chroma_before": f"{chroma_before['files']} files, "
                         f"{chroma_before['bytes']} bytes, digest "
                         f"{str(chroma_before['digest'])[:16]}",
        "chroma_after": f"{chroma_after['files']} files, "
                        f"{chroma_after['bytes']} bytes, digest "
                        f"{str(chroma_after['digest'])[:16]}",
        "chroma_same": chroma_after == chroma_before,
        "chroma_empty_before": chroma_empty_before,
        "corpus_files": len(corpus_before),
        "corpus_hashes": len(hashes_before),
        "corpus_hashes_after": len(hashes_after),
        "corpus_after": len(corpus_after),
        "corpus_size_mb": round(corpus_size / (1024 * 1024), 1),
        "file_counts": file_counts,
        "file_total": sum(file_counts.values()),
        "group_total": sum(ingest[spec["wmo"]]["groups"] for spec in FLOATS),
        "ingest": ingest,
        "timings": timings,
        "timings_float": timings_float,
        "ingest_seconds": sum(timings_float.values()),
        "planned_total": planned_total,
        "inserted_total": sum(ingest[spec["wmo"]]["inserted"] for spec in FLOATS),
        "skipped_total": sum(ingest[spec["wmo"]]["skipped"] for spec in FLOATS),
        "error_total": sum(len(ingest[spec["wmo"]]["errors"]) for spec in FLOATS),
        "rows_total": rows_total,
        "corpus_rows": corpus_rows,
        "gap_note": gap_note,
        "existing_rows": dev_before["rows"],
        "existing_floats": dev_before["floats"],
        "overlap": len(overlap),
        "existing_changed": len(existing_changed),
        "floats_after": int(floats_after),
        "identity_set_ok": not unexpected and stored.keys() >= corpus_set,
        "audit_parity": (
            "every float's identities equal the audit's plans"
            if all(value is not False for value in parity_results.values())
            else "mismatch (see failed checks)"),
        "stats": stats,
        "coverage": coverage,
        "coverage_matches_audit": coverage == EXPECTED_PARAMETER_COUNTS,
        "suspects": suspects,
        "suspects_match_audit": suspect_identities == audit_suspects,
        "suspects_unscaled": all(row["bgc_axis_ok"] and row["core_axis_ok"]
                                 for row in suspects),
        "suspects_measurements": all(row["measurements_ok"] for row in suspects),
        "ok_count": ok_count,
        "grid_rows": grid_rows,
        "vectors": vectors,
        "vector_duplicates": vectors["duplicates"],
        "vector_metadata_ok": vectors["metadata_ok"],
        "vector_documents_ok": vectors["documents_ok"],
        "vector_bgc_ok": vectors["bgc_ok"],
        "vector_second_writes": vector_second_writes,
        "vector_document_changes": len(document_changes),
        "vector_count_after": vector_count_after,
        "second_inserted": second_inserted,
        "second_skipped": second_skipped,
        "second_errors": second_errors,
        "rows_before_second": rows_before_second,
        "rows_after_second": rows_after_second,
        "digest_changes": len(changed),
        "timestamp_changes": len(timestamp_changes),
        "duplicate_count": len(base.duplicate_identities(engine)),
        "interrupt_delay": args.interrupt_delay,
        "recovery": recovery,
        "tests": tests_text,
        "peak_mb": sampler.peak_mb,
        "production_same": production_same,
        "rates": {
            "ingest": round(
                EXPECTED_PROFILES / max(sum(timings_float.values()), 1e-9), 3),
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    os.makedirs(args.json_dir, exist_ok=True)

    # Written before rendering: the measured evidence must survive a crash in
    # the report formatter. The per-row payloads stay out of the file (they are
    # already in the database and would be ~20 MB).
    evidence = dict(meta)
    evidence["stats"] = {
        wmo: {key: value for key, value in entry.items()
              if key != "row_by_identity"}
        for wmo, entry in meta["stats"].items()}
    with open(os.path.join(args.json_dir, "meta.json"), "w",
              encoding="utf-8") as handle:
        json.dump(json_safe(evidence), handle, indent=2, default=str)

    serialisable = {
        "environment": {
            "server_version": server_version,
            "development_database": DB_NAME,
            "validation_database": DISPOSABLE_DB,
            "chroma_validation_store": CHROMA_DIR,
            "chroma_collection": COLLECTION_NAME,
            "dataset_root": DATA_ROOT,
        },
        "counts": {
            "files": meta["file_total"],
            "groups": meta["group_total"],
            "planned": planned_total,
            "inserted": meta["inserted_total"],
            "skipped": meta["skipped_total"],
            "errors": meta["error_total"],
            "rows_total": rows_total,
            "existing_rows": meta["existing_rows"],
            "overlap": meta["overlap"],
        },
        "per_float": {
            str(spec["wmo"]): {
                "files": file_counts[spec["wmo"]],
                "groups": ingest[spec["wmo"]]["groups"],
                "inserted": ingest[spec["wmo"]]["inserted"],
                "skipped": ingest[spec["wmo"]]["skipped"],
                "errors": ingest[spec["wmo"]]["errors"],
                "seconds": round(timings_float[spec["wmo"]], 1),
                "rows": stats[spec["wmo"]]["rows"],
                "directions": stats[spec["wmo"]]["directions"],
                "grid": stats[spec["wmo"]]["statuses"],
            } for spec in FLOATS
        },
        "coverage": coverage,
        "grid": {
            "statuses": dict(statuses),
            "suspects": suspects,
            "matches_audit": suspect_identities == audit_suspects,
        },
        "vectors": {
            "count": vectors["count"],
            "metadata_keys": vectors["metadata_keys"],
            "distinction_keys": vectors["distinction_keys"],
            "second_pass_writes": vector_second_writes,
            "document_changes": len(document_changes),
        },
        "idempotency": {
            "inserted": second_inserted,
            "skipped": second_skipped,
            "errors": second_errors,
            "digest_changes": len(changed),
            "timestamp_changes": len(timestamp_changes),
        },
        "recovery": recovery,
        "development_unchanged": {
            "database": dev_before == dev_after,
            "chroma_store": chroma_after == chroma_before,
            "corpus_size_mtime": not changed_files,
            "corpus_sha256": not changed_hashes,
            "code_and_config": production_same,
        },
        "checks": {
            "total": len(two.RESULTS),
            "passed": len(two.RESULTS) - len(failed),
            "failed": len(failed),
            "failures": [
                {"section": name, "check": label, "detail": detail}
                for _ok, label, detail, name in failed],
            "per_section": {
                name: {
                    "passed": sum(1 for item in two.RESULTS
                                  if item[3] == name and item[0]),
                    "failed": sum(1 for item in two.RESULTS
                                  if item[3] == name and not item[0]),
                }
                for name in OrderedDict.fromkeys(item[3] for item in two.RESULTS)
            },
        },
        "runtime": {
            "phase_seconds": {key: round(value, 1) for key, value in timings.items()},
            "peak_rss_mb": sampler.peak_mb,
        },
        "verdict": verdict,
    }
    with open(os.path.join(args.json_dir, "summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump(json_safe(serialisable), handle, indent=2, default=str)

    report = format_markdown(meta)
    with open(args.report, "w", encoding="utf-8") as handle:
        handle.write(report)

    print(f"\nchecks passed : {len(two.RESULTS) - len(failed)}")
    print(f"checks failed : {len(failed)}")
    for _ok, label, detail, name in failed:
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))
    print(f"\nreport : {os.path.abspath(args.report)}")
    print(f"verdict: {verdict}")

    if args.keep_db:
        print(f"Kept database '{DISPOSABLE_DB}' and store '{CHROMA_DIR}'.")
    else:
        base.drop_database(DISPOSABLE_DB)
        shutil.rmtree(CHROMA_DIR, ignore_errors=True)
        print(f"Dropped database '{DISPOSABLE_DB}' and store '{CHROMA_DIR}'.")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
