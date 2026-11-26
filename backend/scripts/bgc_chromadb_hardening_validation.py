"""Validate the ChromaDB BGC metadata and missing-vector recovery fixes.

This is a verification tool, not a test module. It never writes to the
development database, its ChromaDB store or the NetCDF corpus: the relational
data goes to the disposable database ``intelliquery_bgc_hardening_check`` and
the vectors to ``scratch/bgc_hardening/chroma_store``.

What is exercised, all through the production ``ArgoDataProcessor`` and
``DatabaseManager`` paths:

  1. isolated database schema and an empty isolated vector store
  2. metadata written for core-only and core+BGC profiles
  3. the exact crash window — MySQL commit done, vector write prevented by a
     deterministic injected exception (not a sleep)
  4. re-ingestion repairs exactly the missing vector, adds no row and no
     duplicate vector
  5. a third run changes neither a row payload nor a vector
  6. a failed reconciliation is reported and a later run retries it
  7. metadata filters (has_bgc, product_type, bgc_parameters, count)
  8. a semantic query still resolves to relational rows (core retrieval)
  9. optional ``--full-corpus``: the whole 1,264-file corpus, its vector
     metadata, and reconciliation at corpus scale

Run from ``backend/``:

    python scripts/bgc_chromadb_hardening_validation.py
    python scripts/bgc_chromadb_hardening_validation.py --full-corpus
    python scripts/bgc_chromadb_hardening_validation.py --keep-db

Exit status is 0 only when every check passes.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time

import chromadb
from sentence_transformers import SentenceTransformer
from sqlalchemy import text

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))
import bgc_controlled_ingestion as base  # noqa: E402

from bgc_matching import index_file, profile_time_from_juld  # noqa: E402
from config import CHROMA_PERSIST_DIR, DB_NAME  # noqa: E402
from data_processor import ArgoDataProcessor  # noqa: E402
from database_manager import DatabaseManager  # noqa: E402

DISPOSABLE_DB = "intelliquery_bgc_hardening_check"
CHROMA_DIR = os.path.join(BACKEND_DIR, "scratch", "bgc_hardening", "chroma_store")
COLLECTION = "argo_float_profiles_hardening_check"
JSON_DIR = os.path.join(BACKEND_DIR, "scratch", "bgc_hardening")
DATA_ROOT = base.DATA_ROOT

#: Expected full-corpus totals, from the validated full-corpus run.
EXPECTED_PROFILES = 632
EXPECTED_GROUPS = 626
EXPECTED_COVERAGE = {
    "DOXY": 619, "CHLA": 619, "BBP700": 619,
    "NITRATE": 126, "PH_IN_SITU_TOTAL": 316,
}
EXPECTED_SUSPECTS = 13

CHECKS = []


def check(ok, label, detail=""):
    CHECKS.append({"ok": bool(ok), "label": label, "detail": str(detail)})
    print(("PASS  " if ok else "FAIL  ") + label +
          ("" if not detail else f"  [{detail}]"))
    return bool(ok)


def section(title):
    print("\n" + title)
    print("-" * len(title))


class InjectedVectorFailure(RuntimeError):
    """The controlled failure at the MySQL-commit / vector-write boundary."""


class HardeningManager(DatabaseManager):
    """Production ``DatabaseManager`` on a disposable database and store.

    ``DatabaseManager.__init__`` is skipped so the configured (development)
    ChromaDB directory is never opened. The failure hooks raise at exact,
    selected points: the first vector write of one chosen profile, or the
    reconciliation of one chosen profile id.
    """

    def __init__(self, engine, chroma_dir, collection_name):
        self.mysql_engine = engine
        self.chroma_client = chromadb.PersistentClient(path=chroma_dir)
        self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        self.chroma_collection = self.chroma_client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"})
        self.fail_vector_time = None
        self.fail_sync_ids = set()
        self.crashed_profile_ids = []
        self.vector_writes = 0
        self.sync_results = []

    def add_profile_to_chromadb(self, profile_id, float_id, cycle, time,
                                lat, lon, bgc_keys, pressure, temperature,
                                salinity, direction=None):
        if self.fail_vector_time is not None and time == self.fail_vector_time:
            self.crashed_profile_ids.append(int(profile_id))
            raise InjectedVectorFailure(
                f"injected: profile {profile_id} was committed to MySQL and "
                "its ChromaDB write was prevented")
        DatabaseManager.add_profile_to_chromadb(
            self, profile_id, float_id, cycle, time, lat, lon, bgc_keys,
            pressure, temperature, salinity, direction=direction)
        self.vector_writes += 1

    def sync_profile_vector(self, profile_id):
        if int(profile_id) in self.fail_sync_ids:
            raise InjectedVectorFailure(
                f"injected: reconciliation of profile {profile_id} failed")
        status = DatabaseManager.sync_profile_vector(self, profile_id)
        self.sync_results.append((int(profile_id), status))
        return status


# ---------------------------------------------------------------------------
# Read-only helpers
# ---------------------------------------------------------------------------

def directory_fingerprint(directory):
    """File count, byte count and digest of a directory tree, read only."""
    if not os.path.isdir(directory):
        return {"exists": False, "files": 0, "bytes": 0, "digest": None}
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
    return {"exists": True, "files": files, "bytes": total,
            "digest": digest.hexdigest()}


def development_fingerprint():
    engine = base.engine_for(DB_NAME)
    with engine.connect() as conn:
        profiles = conn.execute(
            text("SELECT COUNT(*) FROM argo_profiles")).scalar()
        floats = conn.execute(
            text("SELECT COUNT(*) FROM argo_floats")).scalar()
    engine.dispose()
    return {"profiles": int(profiles), "floats": int(floats),
            "chroma": directory_fingerprint(CHROMA_PERSIST_DIR)}


def corpus_fingerprint():
    digest = hashlib.sha256()
    files = 0
    total = 0
    for root, _dirs, names in os.walk(DATA_ROOT):
        for name in sorted(names):
            if not name.lower().endswith(".nc"):
                continue
            path = os.path.join(root, name)
            info = os.stat(path)
            files += 1
            total += info.st_size
            digest.update(os.path.relpath(path, DATA_ROOT).encode("utf-8"))
            digest.update(str(info.st_size).encode("ascii"))
    return {"files": files, "bytes": total, "digest": digest.hexdigest()}


def json_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def sql_rows(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.profile_id, p.float_id, f.wmo_number, p.cycle_number,
                   p.profile_time, p.direction, p.latitude, p.longitude,
                   p.pressure, p.temperature, p.salinity, p.bgc_params,
                   p.bgc_source_metadata
            FROM argo_profiles p
            JOIN argo_floats f ON f.float_id = p.float_id
        """)).mappings().fetchall()
    return sorted(rows, key=lambda row: row["profile_id"])


def row_count(engine):
    with engine.connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM argo_profiles")).scalar())


def duplicate_identities(engine):
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT float_id, cycle_number, direction, profile_time, COUNT(*) n
            FROM argo_profiles
            GROUP BY float_id, cycle_number, direction, profile_time
            HAVING n > 1
        """)).fetchall()


def sql_fingerprint(engine):
    """Changes when any profile value changes; identity and payload lengths."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(CRC32(CONCAT_WS('|', profile_id, float_id,
                       cycle_number, profile_time, latitude, longitude,
                       direction))), 0) AS identity_hash,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(pressure AS CHAR)), 0)), 0)
                       AS pres,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(temperature AS CHAR)), 0)), 0)
                       AS temp,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(salinity AS CHAR)), 0)), 0)
                       AS psal,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(bgc_params AS CHAR)), 0)), 0)
                       AS bgc,
                   COALESCE(SUM(COALESCE(LENGTH(CAST(bgc_pressure AS CHAR)), 0)), 0)
                       AS bgc_pres
            FROM argo_profiles
        """)).mappings().fetchone()
    return dict(row)


def vector_records(collection):
    data = collection.get(include=["documents", "metadatas"])
    return {
        item: (document, metadata)
        for item, document, metadata in zip(
            data["ids"], data["documents"], data["metadatas"])
    }


def vector_digest(collection):
    records = vector_records(collection)
    digest = hashlib.sha256()
    for item in sorted(records):
        document, metadata = records[item]
        digest.update(item.encode("utf-8"))
        digest.update(document.encode("utf-8"))
        digest.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
    return digest.hexdigest(), len(records)


def parameter_names(row):
    params = json_value(row["bgc_params"]) or {}
    return sorted(params) if isinstance(params, dict) else []


def expected_metadata(row):
    """Metadata derived from SQL alone, independently of the production code."""
    names = parameter_names(row)
    metadata = {
        "profile_id_sql": int(row["profile_id"]),
        "float_id_sql": int(row["wmo_number"]),
        "cycle_number": int(row["cycle_number"]),
        "date": row["profile_time"].strftime("%Y-%m-%d"),
        "has_bgc": bool(names),
        "product_type": "core_bgc" if names else "core",
        "bgc_parameters": ",".join(names),
        "bgc_parameter_count": len(names),
    }
    if row["latitude"] is not None and row["longitude"] is not None:
        metadata["latitude"] = float(row["latitude"])
        metadata["longitude"] = float(row["longitude"])
    if row["direction"] in ("A", "D"):
        metadata["direction"] = row["direction"]
    return metadata, names


def verify_vectors(collection, rows):
    """Every row has exactly one vector whose content matches the row."""
    records = vector_records(collection)
    expected_ids = {f"profile_{int(row['profile_id'])}" for row in rows}
    check(set(records) == expected_ids,
          "vector ids equal the relational profile ids",
          f"{len(records)} vectors, {len(expected_ids)} rows")

    mismatched = []
    document_problems = []
    for row in rows:
        item = f"profile_{int(row['profile_id'])}"
        if item not in records:
            continue
        document, metadata = records[item]
        expected, names = expected_metadata(row)
        if dict(metadata or {}) != expected:
            mismatched.append(item)
        if str(int(row["profile_id"])) not in document:
            document_problems.append(item)
        for name in names:
            if name not in document:
                document_problems.append(item)

    check(not mismatched,
          "every vector metadata matches its relational row",
          f"{len(mismatched)} mismatched" +
          ("" if not mismatched else f", e.g. {mismatched[:3]}"))
    check(not document_problems,
          "every document names its profile and BGC parameters",
          f"{len(document_problems)} problems")

    counts = {}
    for metadata in [record[1] for record in records.values()]:
        counts[metadata.get("product_type")] = \
            counts.get(metadata.get("product_type"), 0) + 1
    return counts


def statuses(manager):
    result = {}
    for _profile_id, status in manager.sync_results:
        result[status] = result.get(status, 0) + 1
    return result


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def setup(args):
    section("Setup: safety, isolated database and empty vector store")

    check(DB_NAME != DISPOSABLE_DB,
          "the disposable database is not the development database",
          DISPOSABLE_DB)
    check(os.path.abspath(CHROMA_DIR) != os.path.abspath(CHROMA_PERSIST_DIR),
          "the isolated store is not the development ChromaDB directory")

    before = {
        "development": development_fingerprint(),
        "corpus": corpus_fingerprint(),
    }
    check(before["development"]["profiles"] > 0,
          "development database is readable (fingerprinted only)",
          f"{before['development']['profiles']} profiles, "
          f"{before['development']['floats']} floats")

    base.reset_database(DISPOSABLE_DB)
    engine = base.engine_for(DISPOSABLE_DB)
    schema_manager = base.DisposableDatabaseManager(engine)
    base.build_schema(schema_manager)

    columns = schema_manager._get_profile_table_columns()
    keys = schema_manager._get_profile_unique_keys()
    check(len(columns) == 39, "disposable database has the 39 profile columns",
          f"{len(columns)} columns")
    check(keys == (DatabaseManager.PROFILE_IDENTITY_KEY,),
          "its only unique key is the logical identity", str(keys))
    check(row_count(engine) == 0, "the disposable database starts empty")

    if os.path.isdir(CHROMA_DIR):
        shutil.rmtree(CHROMA_DIR)
    os.makedirs(CHROMA_DIR, exist_ok=True)

    manager = HardeningManager(engine, CHROMA_DIR, COLLECTION)
    check(manager.chroma_collection.count() == 0,
          "the isolated vector collection starts empty")

    return engine, manager, before


def representative_paths():
    return [os.path.join(DATA_ROOT, rel) for rel in base.INGEST_FILES]


def phase_crash_window(engine, manager):
    section("1. MySQL commit succeeds, the ChromaDB write is prevented")

    target_file = os.path.join(
        DATA_ROOT, "incois", "3902581", "profiles", "D3902581_001.nc")
    records = index_file(target_file)
    target_time = profile_time_from_juld(records[0]["juld"])
    check(target_time is not None,
          "a target profile time was resolved from the source file",
          str(target_time))

    manager.fail_vector_time = target_time
    processor = ArgoDataProcessor(manager)
    summary = processor.process_paths(representative_paths())

    check(manager.crashed_profile_ids != [],
          "the injected failure was reached at the vector-write boundary",
          f"profile ids {manager.crashed_profile_ids}")
    check(summary["inserted"] == 10 and len(summary["errors"]) == 1,
          "the run stored every other profile and reported the failure",
          f"inserted={summary['inserted']} skipped={summary['skipped']} "
          f"errors={len(summary['errors'])}")

    rows = sql_rows(engine)
    missing = [
        int(row["profile_id"]) for row in rows
        if f"profile_{int(row['profile_id'])}"
        not in vector_records(manager.chroma_collection)
    ]
    check(row_count(engine) == 11,
          "the failed profile's relational row was committed", "11 rows")
    check(missing == manager.crashed_profile_ids,
          "exactly the committed profile has no ChromaDB vector",
          f"missing={missing}")
    check(manager.chroma_collection.count() == 10,
          "the other ten profiles kept their vectors", "10 vectors")

    if missing:
        row = next(r for r in rows if int(r["profile_id"]) == missing[0])
        check(json_value(row["pressure"]) not in (None, []),
              "the committed row holds its complete measurements")
        check(json_value(row["bgc_params"]) not in (None, {}),
              "the committed row holds its BGC measurements")

    manager.fail_vector_time = None
    return summary


def phase_repair(engine, manager, before):
    section("2. Re-ingestion repairs the missing vector without a duplicate")

    digest_before, _ = vector_digest(manager.chroma_collection)
    fingerprint_before = sql_fingerprint(engine)

    manager.sync_results = []
    processor = ArgoDataProcessor(manager)
    summary = processor.process_paths(representative_paths())

    check(summary["inserted"] == 0 and summary["skipped"] == 11
          and not summary["errors"],
          "the second run inserted no row and reported no error",
          f"inserted={summary['inserted']} skipped={summary['skipped']} "
          f"errors={len(summary['errors'])}")
    check(sql_fingerprint(engine) == fingerprint_before,
          "no stored payload changed on the second run")
    check(duplicate_identities(engine) == [],
          "no duplicate logical identity exists")

    rows = sql_rows(engine)
    check(vector_records(manager.chroma_collection).keys()
          == {f"profile_{int(row['profile_id'])}" for row in rows},
          "every relational row now has exactly one vector",
          f"{manager.chroma_collection.count()} vectors / {len(rows)} rows")
    check(statuses(manager) == {"recreated": 1, "unchanged": 10},
          "exactly one vector was recreated and ten left untouched",
          str(statuses(manager)))

    digest_after, _ = vector_digest(manager.chroma_collection)
    check(digest_after != digest_before,
          "the vector store changed only by the repaired vector")

    counts = verify_vectors(manager.chroma_collection, rows)
    check(counts.get("core", 0) == 1 and counts.get("core_bgc", 0) == 10,
          "one core-only and ten core+BGC profiles are present",
          str(counts))
    return summary


def phase_idempotent(engine, manager):
    section("3. A third run changes neither a row nor a vector")

    fingerprint_before = sql_fingerprint(engine)
    digest_before, _ = vector_digest(manager.chroma_collection)
    writes_before = manager.vector_writes
    manager.sync_results = []

    processor = ArgoDataProcessor(manager)
    summary = processor.process_paths(representative_paths())

    check(summary["inserted"] == 0 and summary["skipped"] == 11
          and not summary["errors"],
          "the third run inserted nothing and reported no error",
          f"inserted={summary['inserted']} skipped={summary['skipped']}")
    check(sql_fingerprint(engine) == fingerprint_before,
          "every stored payload is byte-for-byte unchanged")
    digest_after, _ = vector_digest(manager.chroma_collection)
    check(digest_after == digest_before,
          "the whole vector store is unchanged")
    check(statuses(manager) == {"unchanged": 11},
          "all eleven existing vectors were left untouched",
          str(statuses(manager)))
    check(manager.vector_writes == writes_before,
          "no new vector was written on the third run",
          f"{manager.vector_writes} writes in total")


def phase_failed_reconciliation(engine, manager):
    section("4. A failed reconciliation is reported and retried")

    rows = sql_rows(engine)
    victim = f"profile_{int(rows[0]['profile_id'])}"
    manager.chroma_collection.delete(ids=[victim])
    check(victim not in vector_records(manager.chroma_collection),
          "one vector was removed to simulate a lost vector", victim)

    manager.sync_results = []
    manager.fail_sync_ids = {int(rows[0]["profile_id"])}
    processor = ArgoDataProcessor(manager)
    failed = processor.process_paths(representative_paths())

    check(len(failed["errors"]) == 1 and failed["inserted"] == 0,
          "the failed reconciliation was reported, not swallowed",
          f"errors={len(failed['errors'])} inserted={failed['inserted']}")
    check(victim not in vector_records(manager.chroma_collection),
          "the vector is still missing after the failed reconciliation")
    check(row_count(engine) == 11, "the relational row was not duplicated")

    manager.fail_sync_ids = set()
    manager.sync_results = []
    summary = processor.process_paths(representative_paths())
    check(summary["errors"] == [] and summary["inserted"] == 0,
          "the retry succeeded without inserting a row",
          f"skipped={summary['skipped']}")
    check(statuses(manager).get("recreated") == 1,
          "the retry recreated exactly the missing vector",
          str(statuses(manager)))
    check(verify_vectors(manager.chroma_collection, sql_rows(engine))
          is not None, "the repaired vector matches its relational row")


def phase_filters(engine, manager):
    section("5. Metadata filtering")

    collection = manager.chroma_collection
    rows = sql_rows(engine)
    bgc_rows = [row for row in rows if parameter_names(row)]
    core_rows = [row for row in rows if not parameter_names(row)]

    bgc_ids = set(collection.get(where={"has_bgc": True})["ids"])
    core_ids = set(collection.get(where={"has_bgc": False})["ids"])
    check(bgc_ids == {f"profile_{int(row['profile_id'])}" for row in bgc_rows},
          "has_bgc = true selects exactly the BGC-bearing profiles",
          f"{len(bgc_ids)} ids")
    check(core_ids == {f"profile_{int(row['profile_id'])}" for row in core_rows},
          "has_bgc = false selects exactly the core-only profiles",
          f"{len(core_ids)} ids")

    for product_type, expected in (("core", core_rows),
                                   ("core_bgc", bgc_rows)):
        ids = set(collection.get(where={"product_type": product_type})["ids"])
        check(ids == {f"profile_{int(row['profile_id'])}"
                      for row in expected},
              f"product_type = {product_type} selects its profiles",
              f"{len(ids)} ids")

    sample = bgc_rows[0]
    names = parameter_names(sample)
    ids = set(collection.get(
        where={"bgc_parameters": ",".join(names)})["ids"])
    check(f"profile_{int(sample['profile_id'])}" in ids,
          "bgc_parameters allows an exact parameter-set filter",
          ",".join(names))
    ids = set(collection.get(
        where={"bgc_parameter_count": {"$gte": len(names)}})["ids"])
    check(f"profile_{int(sample['profile_id'])}" in ids,
          "bgc_parameter_count allows a range filter", f">= {len(names)}")

    for row in bgc_rows:
        metadata = collection.get(ids=[f"profile_{int(row['profile_id'])}"],
                                  include=["metadatas"])["metadatas"][0]
        names = parameter_names(row)
        if metadata["bgc_parameters"] != ",".join(names):
            check(False, "bgc_parameters equals the keys of bgc_params",
                  f"profile {row['profile_id']}")
            return
    check(True, "bgc_parameters equals the keys of bgc_params for every row",
          f"{len(bgc_rows)} BGC rows")


def phase_retrieval(manager):
    section("6. Core retrieval through the vector store")

    collection = manager.chroma_collection
    embedding = manager.embedding_model.encode(
        "oxygen and chlorophyll measurements near the surface").tolist()
    result = collection.query(query_embeddings=[embedding], n_results=5)

    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    check(len(ids) == min(5, collection.count()),
          "a semantic query returns profiles", f"{len(ids)} results")
    profile_ids = [int(metadata["profile_id_sql"]) for metadata in metadatas]
    check(all("profile_id_sql" in metadata for metadata in metadatas),
          "every semantic result still exposes profile_id_sql",
          str(profile_ids))

    engine = manager.mysql_engine
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT COUNT(*) FROM argo_profiles WHERE profile_id IN "
                 "(" + ",".join(str(value) for value in profile_ids) + ")")
        ).scalar()
    check(int(found) == len(profile_ids),
          "every returned id resolves to a relational profile",
          f"{found}/{len(profile_ids)}")

    core_only = collection.get(where={"has_bgc": False})["ids"]
    check(bool(core_only),
          "a core-only profile is still retrievable when filtered",
          str(core_only))


def phase_full_corpus(engine, manager):
    section("7. Full corpus compatibility")

    check(row_count(engine) == 11,
          "corpus ingestion starts from the eleven representative rows")

    started = time.time()
    manager.sync_results = []
    processor = ArgoDataProcessor(manager)
    summary = processor.process_dataset(DATA_ROOT)
    elapsed = time.time() - started

    check(summary["errors"] == [],
          "corpus ingestion reported no error",
          f"{len(summary['errors'])} errors")
    check(summary["inserted"] == EXPECTED_PROFILES - 11
          and summary["skipped"] == 11,
          "the remaining 621 profiles were inserted, the 11 skipped",
          f"inserted={summary['inserted']} skipped={summary['skipped']} "
          f"groups={summary['groups']}")
    check(summary["groups"] == EXPECTED_GROUPS,
          "the corpus resolves to 626 (float, cycle) groups",
          str(summary["groups"]))
    check(row_count(engine) == EXPECTED_PROFILES,
          "632 relational rows exist", f"{row_count(engine)} rows")
    check(manager.chroma_collection.count() == EXPECTED_PROFILES,
          "632 vectors exist", f"{manager.chroma_collection.count()}")
    check(duplicate_identities(engine) == [], "no duplicate logical identity")

    rows = sql_rows(engine)
    counts = verify_vectors(manager.chroma_collection, rows)
    check(counts.get("core_bgc", 0) + counts.get("core", 0)
          == EXPECTED_PROFILES,
          "every vector is classified as core or core_bgc", str(counts))

    coverage = {name: 0 for name in EXPECTED_COVERAGE}
    for row in rows:
        for name in parameter_names(row):
            if name in coverage:
                coverage[name] += 1
    check(coverage == EXPECTED_COVERAGE,
          "per-parameter coverage matches the validated corpus",
          str(coverage))

    with engine.connect() as conn:
        suspects = conn.execute(text("""
            SELECT COUNT(*) FROM argo_profiles
            WHERE JSON_UNQUOTE(JSON_EXTRACT(bgc_source_metadata,
                  '$.grid_check')) = 'pres_scale_suspect'
        """)).scalar()
    check(int(suspects) == EXPECTED_SUSPECTS,
          "all 13 pressure-grid anomalies remain flagged", str(suspects))

    fingerprint_before = sql_fingerprint(engine)
    digest_before, _ = vector_digest(manager.chroma_collection)

    victims = [f"profile_{int(row['profile_id'])}" for row in rows[:3]]
    manager.chroma_collection.delete(ids=victims)
    check(manager.chroma_collection.count() == EXPECTED_PROFILES - 3,
          "three corpus vectors were removed to simulate a loss",
          str(manager.chroma_collection.count()))

    # The ingestion path uses this same method for an existing row; calling it
    # directly here exercises corpus-scale reconciliation without re-reading
    # the 1,264 NetCDF files a third time.
    manager.sync_results = []
    for row in rows:
        manager.sync_profile_vector(int(row["profile_id"]))
    check(statuses(manager) == {"recreated": 3,
                                "unchanged": EXPECTED_PROFILES - 3},
          "exactly the three deleted vectors were recreated",
          str(statuses(manager)))
    check(sql_fingerprint(engine) == fingerprint_before,
          "reconciliation changed no relational payload")
    digest_after, _ = vector_digest(manager.chroma_collection)
    check(digest_after == digest_before,
          "the rebuilt vectors are identical to the deleted ones")

    manager.sync_results = []
    for row in rows:
        manager.sync_profile_vector(int(row["profile_id"]))
    check(statuses(manager) == {"unchanged": EXPECTED_PROFILES},
          "a further reconciliation leaves every corpus vector unchanged",
          str(statuses(manager)))
    return {"seconds": round(elapsed, 1),
            "inserted": summary["inserted"],
            "skipped": summary["skipped"],
            "rows": row_count(engine),
            "vectors": manager.chroma_collection.count(),
            "product_types": counts,
            "coverage": coverage,
            "suspects": int(suspects)}


def teardown(engine, manager, before, args, evidence):
    section("8. Development environment unchanged")

    after = {
        "development": development_fingerprint(),
        "corpus": corpus_fingerprint(),
    }
    check(after["development"]["profiles"] == before["development"]["profiles"]
          and after["development"]["floats"] == before["development"]["floats"],
          "the development database row and float counts are unchanged",
          f"{after['development']['profiles']} / "
          f"{after['development']['floats']}")
    check(after["development"]["chroma"] == before["development"]["chroma"],
          "the development ChromaDB store is unchanged",
          f"{after['development']['chroma']['bytes']} bytes")
    check(after["corpus"] == before["corpus"],
          "the NetCDF corpus is unchanged",
          f"{after['corpus']['files']} files")

    manager.chroma_client = None
    engine.dispose()
    if not args.keep_db:
        base.drop_database(DISPOSABLE_DB)
        check(True, "the disposable database was dropped")

    os.makedirs(JSON_DIR, exist_ok=True)
    passed = sum(1 for item in CHECKS if item["ok"])
    evidence.update({
        "checks": CHECKS,
        "counts": {"total": len(CHECKS), "passed": passed,
                   "failed": len(CHECKS) - passed},
    })
    with open(os.path.join(JSON_DIR, "checks.json"), "w",
              encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, default=str)
    with open(os.path.join(JSON_DIR, "run.log"), "w",
              encoding="utf-8") as handle:
        for item in CHECKS:
            handle.write(("PASS\t" if item["ok"] else "FAIL\t")
                         + item["label"] + "\t" + item["detail"] + "\n")

    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return passed == len(CHECKS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true",
                        help="keep the disposable database after the run")
    parser.add_argument("--full-corpus", action="store_true",
                        help="also ingest the full 1,264-file corpus")
    args = parser.parse_args()

    evidence = {"environment": {"development_database": DB_NAME,
                                "disposable_database": DISPOSABLE_DB,
                                "chroma_store": CHROMA_DIR,
                                "collection": COLLECTION,
                                "dataset": DATA_ROOT,
                                "full_corpus": bool(args.full_corpus)}}
    started = time.time()

    engine, manager, before = setup(args)
    try:
        phase_crash_window(engine, manager)
        phase_repair(engine, manager, before)
        phase_idempotent(engine, manager)
        phase_failed_reconciliation(engine, manager)
        phase_filters(engine, manager)
        phase_retrieval(manager)
        if args.full_corpus:
            evidence["full_corpus"] = phase_full_corpus(engine, manager)
    finally:
        evidence["seconds"] = round(time.time() - started, 1)
        ok = teardown(engine, manager, before, args, evidence)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
