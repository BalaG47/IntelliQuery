"""Focused tests for BGC validation exception corrections and dev promotion.

Covers:
  * targeted complete-group reprocessing of ``6990679`` cycle 1 (``core_bgc``
    with DOXY, CHLA, BBP700) and ``7902200`` cycle 15 (``pres_scale_suspect``
    with preserved raw pressure axis);
  * dry-run non-mutation default CLI behavior;
  * identity collision detection, foreign-key parent creation, and existing-row
    preservation;
  * SQL/vector interruption recovery (Stage A) and backfill idempotency /
    partial failure + retry (Stage B);
  * real backup artifact verification failures and valid backup acceptance.

Run from ``backend/``::

    python -m unittest test_bgc_promotion_and_correction -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import chromadb
from sqlalchemy import create_engine, text

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BACKEND_DIR, "scripts")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import bgc_promote_to_dev as promote  # noqa: E402
from bgc_matching import (  # noqa: E402
    GRID_OK,
    GRID_PRES_SCALE_SUSPECT,
    MATCH_MATCHED,
    group_by_cycle,
    index_file,
    plan_group,
)
from data_processor import ArgoDataProcessor  # noqa: E402
from database_manager import DatabaseManager  # noqa: E402

BGC_DATA_ROOT = os.environ.get(
    "BGC_DATA_DIR", os.path.join(BACKEND_DIR, "bgc data", "bgc raw data")
)
if not os.path.isabs(BGC_DATA_ROOT):
    BGC_DATA_ROOT = os.path.join(BACKEND_DIR, BGC_DATA_ROOT)

EXCEPTION_FILES = (
    os.path.join(BGC_DATA_ROOT, "incois", "6990679", "profiles", "R6990679_001.nc"),
    os.path.join(BGC_DATA_ROOT, "incois", "6990679", "profiles", "SR6990679_001.nc"),
    os.path.join(BGC_DATA_ROOT, "incois", "7902200", "profiles", "R7902200_015.nc"),
    os.path.join(BGC_DATA_ROOT, "incois", "7902200", "profiles", "SR7902200_015.nc"),
)
CORPUS_EXCEPTIONS_AVAILABLE = all(os.path.isfile(p) for p in EXCEPTION_FILES)


class _Vector(list):
    def tolist(self):
        return list(self)


class _StubEmbeddingModel:
    def __init__(self):
        self.encode_calls = 0

    def encode(self, _document):
        self.encode_calls += 1
        return _Vector([0.1, 0.2, 0.3])


class _InMemoryRelationalChromaManager(DatabaseManager):
    """Uses real ``DatabaseManager`` vector/metadata methods + temp ChromaDB."""

    def __init__(self, chroma_dir, collection_name):
        self.chroma_client = chromadb.PersistentClient(path=chroma_dir)
        self.embedding_model = _StubEmbeddingModel()
        self.chroma_collection = self.chroma_client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"}
        )
        self.floats = {}
        self.wmo_by_float_id = {}
        self.profiles = {}
        self.identities = {}
        self._next_float_id = 1
        self._next_profile_id = 1

    def get_or_create_float(self, wmo_number, project_name, platform_type):
        wmo = int(wmo_number)
        if wmo in self.floats:
            return self.floats[wmo]
        fid = self._next_float_id
        self._next_float_id += 1
        self.floats[wmo] = fid
        self.wmo_by_float_id[fid] = wmo
        return fid

    def get_profile_id(self, float_id, cycle_number, direction, profile_time):
        return self.identities.get(
            (int(float_id), int(cycle_number), direction, profile_time)
        )

    def insert_profile(self, profile_data):
        ident = (
            int(profile_data["float_id"]),
            int(profile_data["cycle_number"]),
            profile_data["direction"],
            profile_data["profile_time"],
        )
        if ident in self.identities:
            raise AssertionError(f"Duplicate insert attempted for {ident}")
        pid = self._next_profile_id
        self._next_profile_id += 1
        row = dict(profile_data)
        row["profile_id"] = pid
        self.profiles[pid] = row
        self.identities[ident] = pid
        return pid

    def get_profile_vector_record(self, profile_id_db):
        row = self.profiles.get(int(profile_id_db))
        if row is None:
            return None

        def decoded(val):
            return json.loads(val) if isinstance(val, str) else val

        fid = int(row["float_id"])
        return {
            "profile_id": int(row["profile_id"]),
            "float_id": fid,
            "wmo_number": self.wmo_by_float_id[fid],
            "cycle_number": int(row["cycle_number"]),
            "profile_time": row["profile_time"],
            "direction": row["direction"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "pressure": decoded(row["pressure"]),
            "temperature": decoded(row["temperature"]),
            "salinity": decoded(row["salinity"]),
            "bgc_params": decoded(row["bgc_params"]) or {},
        }


def _create_sqlite_manager(chroma_dir, collection_name):
    """Create a real SQLite-backed ``DatabaseManager`` + temp ChromaDB."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE argo_floats (
                float_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wmo_number INTEGER UNIQUE NOT NULL,
                project_name TEXT,
                platform_type TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE argo_profiles (
                profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                float_id INTEGER NOT NULL,
                cycle_number INTEGER NOT NULL,
                direction TEXT NOT NULL DEFAULT '-',
                profile_time TIMESTAMP NOT NULL,
                latitude REAL,
                longitude REAL,
                pressure TEXT,
                temperature TEXT,
                salinity TEXT,
                bgc_params TEXT,
                source_file TEXT,
                source_file_sha256 TEXT,
                profile_index_in_file INTEGER NOT NULL DEFAULT 0,
                source_metadata TEXT,
                data_mode TEXT,
                profile_pres_qc TEXT,
                profile_temp_qc TEXT,
                profile_psal_qc TEXT,
                pressure_qc TEXT,
                pressure_adjusted TEXT,
                pressure_adjusted_error TEXT,
                pressure_adjusted_qc TEXT,
                temperature_qc TEXT,
                temperature_adjusted TEXT,
                temperature_adjusted_error TEXT,
                temperature_adjusted_qc TEXT,
                salinity_qc TEXT,
                salinity_adjusted TEXT,
                salinity_adjusted_error TEXT,
                salinity_adjusted_qc TEXT,
                scientific_calib TEXT,
                parameter_data_mode TEXT,
                bgc_qc TEXT,
                bgc_adjusted TEXT,
                bgc_adjusted_error TEXT,
                bgc_adjusted_qc TEXT,
                bgc_pressure TEXT,
                bgc_source_metadata TEXT,
                FOREIGN KEY (float_id) REFERENCES argo_floats(float_id),
                UNIQUE (float_id, cycle_number, direction, profile_time)
            )
        """))

    manager = DatabaseManager.__new__(DatabaseManager)
    manager.mysql_engine = engine
    manager.chroma_client = chromadb.PersistentClient(path=chroma_dir)
    manager.embedding_model = _StubEmbeddingModel()
    manager.chroma_collection = manager.chroma_client.get_or_create_collection(
        name=collection_name, metadata={"hnsw:space": "cosine"}
    )
    manager._cached_profile_cols = set(DatabaseManager.REQUIRED_PROFILE_COLUMNS) | {"profile_id"}
    manager._cached_profile_unique_keys = (DatabaseManager.PROFILE_IDENTITY_KEY,)

    orig_get_record = manager.get_profile_vector_record

    def _sqlite_get_record(pid):
        rec = orig_get_record(pid)
        if rec is not None and isinstance(rec.get("profile_time"), str):
            rec["profile_time"] = datetime.fromisoformat(rec["profile_time"])
        return rec

    manager.get_profile_vector_record = _sqlite_get_record
    return manager


@unittest.skipUnless(
    CORPUS_EXCEPTIONS_AVAILABLE,
    "BGC NetCDF files for 6990679_001 and 7902200_015 not available",
)
class ExceptionGroupCorrectionTests(unittest.TestCase):
    """Complete-group ingestion resolves both partial-subset exceptions."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="bgc_exc_corr_")
        cls.manager = _InMemoryRelationalChromaManager(
            cls.temp_dir, "bgc_exception_test"
        )
        cls.processor = ArgoDataProcessor(cls.manager)
        cls.summary = cls.processor.process_paths(list(EXCEPTION_FILES))
        cls.resummary = cls.processor.process_paths(list(EXCEPTION_FILES))

    @classmethod
    def tearDownClass(cls):
        cls.manager.chroma_client = None
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _row_for(self, wmo, cycle):
        fid = self.manager.floats[wmo]
        for row in self.manager.profiles.values():
            if int(row["float_id"]) == fid and int(row["cycle_number"]) == cycle:
                return row
        self.fail(f"Profile {wmo} cycle {cycle} not found")

    def test_complete_groups_ingest_without_errors_and_stay_idempotent(self):
        self.assertEqual(self.summary["files"], 4)
        self.assertEqual(self.summary["groups"], 2)
        self.assertEqual(self.summary["inserted"], 2)
        self.assertEqual(self.summary["errors"], [])

        self.assertEqual(self.resummary["inserted"], 0)
        self.assertEqual(self.resummary["skipped"], 2)
        self.assertEqual(self.resummary["errors"], [])
        self.assertEqual(self.manager.chroma_collection.count(), 2)

    def test_6990679_cycle_1_complete_group_is_core_bgc_with_doxy_chla_bbp700(self):
        records = []
        for path in EXCEPTION_FILES[:2]:
            records.extend(index_file(path))
        groups = group_by_cycle(records)
        grp = groups[("6990679", 1)]
        plans = plan_group(grp["core"], grp["bgc"])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["match"], MATCH_MATCHED)

        row = self._row_for(6990679, 1)
        bgc_params = json.loads(row["bgc_params"])
        bgc_meta = json.loads(row["bgc_source_metadata"])
        self.assertEqual(bgc_meta["match"], MATCH_MATCHED)
        self.assertEqual(bgc_meta["grid_check"], GRID_OK)
        for required in ("DOXY", "CHLA", "BBP700"):
            self.assertIn(required, bgc_params)

        vec = self.manager.chroma_collection.get(
            ids=[f"profile_{row['profile_id']}"], include=["metadatas", "documents"]
        )
        meta = vec["metadatas"][0]
        self.assertIs(meta["has_bgc"], True)
        self.assertEqual(meta["product_type"], DatabaseManager.PRODUCT_TYPE_CORE_BGC)
        for required in ("DOXY", "CHLA", "BBP700"):
            self.assertIn(required, meta["bgc_parameters"].split(","))
            self.assertIn(required, vec["documents"][0])

    def test_7902200_cycle_15_complete_group_is_flagged_pres_scale_suspect(self):
        records = []
        for path in EXCEPTION_FILES[2:]:
            records.extend(index_file(path))
        groups = group_by_cycle(records)
        grp = groups[("7902200", 15)]
        plans = plan_group(grp["core"], grp["bgc"])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["match"], MATCH_MATCHED)

        row = self._row_for(7902200, 15)
        bgc_meta = json.loads(row["bgc_source_metadata"])
        core_pres = [v for v in json.loads(row["pressure"]) if v is not None]
        bgc_pres = [v for v in json.loads(row["bgc_pressure"]) if v is not None]

        self.assertEqual(bgc_meta["match"], MATCH_MATCHED)
        self.assertEqual(bgc_meta["grid_check"], GRID_PRES_SCALE_SUSPECT)
        self.assertGreater(max(core_pres), 1500.0)
        self.assertLess(max(bgc_pres), 5.0)

        vec = self.manager.chroma_collection.get(
            ids=[f"profile_{row['profile_id']}"], include=["metadatas"]
        )
        meta = vec["metadatas"][0]
        self.assertIs(meta["has_bgc"], True)
        self.assertEqual(meta["product_type"], DatabaseManager.PRODUCT_TYPE_CORE_BGC)
        self.assertEqual(meta["bgc_parameters"], "PH_IN_SITU_TOTAL")


class PromotionDryRunAndSafetyTests(unittest.TestCase):
    """Dry-run guards, backup checks, collision handling, and stage recovery."""

    def test_cli_defaults_to_dry_run_without_mutating(self):
        fake_report = {
            "mode": "dry_run",
            "target_database": "argo_ocean_data_fresh",
            "target_chroma_collection": "argo_float_profiles",
            "target_chroma_dir": "/tmp/chroma",
            "schema": {"ok": True, "column_count": 39},
            "development_baseline": {
                "mysql": {"profiles_count": 1384, "floats_count": 22, "fingerprint": {}},
                "chromadb": {"vector_count": 1384},
            },
            "backfill_plan": {"existing_vectors_to_backfill": 1384},
            "corpus_plan": {"files_scanned": 1264, "groups_count": 626, "planned_profiles_count": 632},
            "promotion_collisions_and_fks": {
                "new_floats_to_create": [2902306],
                "exact_identity_collisions_count": 0,
                "eligible_profiles_to_insert": 632,
            },
            "checks": {"ok": True},
            "all_checks_passed": True,
        }
        with mock.patch.object(sys, "argv", ["bgc_promote_to_dev.py"]), \
             mock.patch.object(promote, "run_dry_run", return_value=fake_report) as dry_mock, \
             mock.patch.object(promote, "run_apply") as apply_mock:
            exit_code = promote.main()
        self.assertEqual(exit_code, 0)
        dry_mock.assert_called_once()
        apply_mock.assert_not_called()

    def test_backup_verification_rejects_missing_empty_and_invalid_artifacts(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_bkp_test_")
        try:
            live_chroma = os.path.join(temp_dir, "live_chroma")
            os.makedirs(live_chroma)
            empty_sql = os.path.join(temp_dir, "empty.sql")
            with open(empty_sql, "w", encoding="utf-8"):
                pass
            valid_sql = os.path.join(temp_dir, "valid.sql")
            with open(valid_sql, "w", encoding="utf-8") as f:
                f.write("-- MySQL dump\nCREATE TABLE argo_profiles (id INT);\n")

            chroma_bkp = os.path.join(temp_dir, "chroma_bkp")
            client = chromadb.PersistentClient(path=chroma_bkp)
            col = client.get_or_create_collection("argo_float_profiles")
            col.add(ids=["profile_1"], embeddings=[[0.1, 0.2]], documents=["doc"], metadatas=[{"a": 1}])

            # Missing MySQL backup
            with self.assertRaises(RuntimeError):
                promote.verify_backups(None, chroma_bkp, live_chroma, "argo_float_profiles")
            # Empty MySQL backup
            with self.assertRaises(RuntimeError):
                promote.verify_backups(empty_sql, chroma_bkp, live_chroma, "argo_float_profiles")
            # Live Chroma directory passed as backup
            with self.assertRaises(RuntimeError):
                promote.verify_backups(valid_sql, live_chroma, live_chroma, "argo_float_profiles")
            # Wrong collection name in Chroma backup
            with self.assertRaises(RuntimeError):
                promote.verify_backups(valid_sql, chroma_bkp, live_chroma, "wrong_collection")
            # Insufficient vector count
            with self.assertRaises(RuntimeError):
                promote.verify_backups(valid_sql, chroma_bkp, live_chroma, "argo_float_profiles", expected_min_vectors=2)

            # Valid backup passes
            info = promote.verify_backups(valid_sql, chroma_bkp, live_chroma, "argo_float_profiles", expected_min_vectors=1)
            self.assertEqual(info["chroma_backup_vectors"], 1)
            self.assertGreater(info["mysql_backup_bytes"], 0)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_analyze_promotion_collisions_and_missing_floats(self):
        t1 = datetime(2024, 12, 1, 13, 28, 0)
        t2 = datetime(2024, 12, 2, 13, 28, 0)
        plans_info = {
            "corpus_wmos": [6990679, 7902200],
            "_plans": [
                {"identity": (6990679, 1, "A", t1)},
                {"identity": (6990679, 1, "D", t2)},
                {"identity": (7902200, 15, "A", t1)},
            ],
        }
        dev_state = {
            "_dev_floats": {6990679: 1},
            "_dev_identities": {(6990679, 1, "A", t1): 101},
            "mysql": {"profiles_count": 1384, "floats_count": 22},
        }
        res = promote.analyze_promotion_collisions(plans_info, dev_state)

        self.assertEqual(res["existing_floats_in_dev"], [6990679])
        self.assertEqual(res["new_floats_to_create"], [7902200])
        self.assertEqual(res["exact_identity_collisions_count"], 1)
        self.assertEqual(
            res["exact_identity_collisions"][0]["existing_profile_id"], 101
        )
        self.assertEqual(res["eligible_profiles_to_insert"], 2)
        self.assertEqual(res["projected_total_profiles_after_apply"], 1386)
        self.assertEqual(res["projected_total_floats_after_apply"], 23)

    def test_foreign_key_parent_creation_is_idempotent(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_fk_")
        try:
            manager = _create_sqlite_manager(temp_dir, "fk_test")
            fid1 = manager.get_or_create_float(6990679, "INCOIS", "ARVOR")
            fid2 = manager.get_or_create_float(6990679, "INCOIS", "ARVOR")
            self.assertEqual(fid1, fid2)
            with manager.mysql_engine.connect() as conn:
                cnt = conn.execute(text("SELECT COUNT(*) FROM argo_floats")).scalar()
            self.assertEqual(cnt, 1)
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_existing_row_is_preserved_not_overwritten_on_collision(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_preserve_")
        try:
            manager = _InMemoryRelationalChromaManager(temp_dir, "preserve_test")
            fid = manager.get_or_create_float(1900121, "PROJ", "APEX")
            pt = datetime(2004, 1, 15, 9, 45, 18)
            existing_payload = {
                "float_id": fid,
                "cycle_number": 44,
                "direction": "A",
                "profile_time": pt,
                "latitude": -10.5,
                "longitude": 48.909,
                "pressure": json.dumps([7.8, 8.9]),
                "temperature": json.dumps([25.0, 24.5]),
                "salinity": json.dumps([35.1, 35.2]),
                "bgc_params": None,
            }
            pid = manager.insert_profile(existing_payload)
            manager.add_profile_from_row(pid)

            processor = ArgoDataProcessor(manager)
            incoming_payload = dict(existing_payload)
            incoming_payload["temperature"] = json.dumps([99.0, 99.0])
            status = processor._store_payload(incoming_payload, "1900121")

            self.assertEqual(status, "skipped")
            self.assertEqual(len(manager.profiles), 1)
            self.assertEqual(
                json.loads(manager.profiles[pid]["temperature"]),
                [25.0, 24.5],
            )
        finally:
            manager.chroma_client = None
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(
        CORPUS_EXCEPTIONS_AVAILABLE,
        "BGC NetCDF files for 6990679_001 and 7902200_015 not available",
    )
    def test_stage_a_sql_commit_vector_crash_recovery_and_no_duplicates(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_stage_a_rec_")
        manifest_path = os.path.join(temp_dir, "manifest.json")
        try:
            manager = _create_sqlite_manager(temp_dir, "stage_a_test")
            orig_add = manager.add_profile_from_row
            calls = {"n": 0}

            def fail_second_vector(pid):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated vector write failure")
                return orig_add(pid)

            manager.add_profile_from_row = fail_second_vector
            with self.assertRaises(RuntimeError):
                promote.run_stage_a_ingest(
                    manager,
                    list(EXCEPTION_FILES),
                    expected_total_profiles=2,
                    manifest_path=manifest_path,
                )

            # 2 SQL rows committed, only 1 vector written
            with manager.mysql_engine.connect() as conn:
                sql_count = conn.execute(text("SELECT COUNT(*) FROM argo_profiles")).scalar()
            self.assertEqual(sql_count, 2)
            self.assertEqual(manager.chroma_collection.count(), 1)

            # Restore vector write and resume Stage A: skips existing rows, repairs missing vector
            manager.add_profile_from_row = orig_add
            manifest = promote.run_stage_a_ingest(
                manager,
                list(EXCEPTION_FILES),
                expected_total_profiles=2,
                manifest_path=manifest_path,
            )
            self.assertEqual(manifest["sql_profiles_total"], 2)
            self.assertEqual(manifest["chroma_vectors_total"], 2)
            self.assertEqual(manifest["ingestion_summary"]["inserted"], 0)
            self.assertEqual(manifest["ingestion_summary"]["skipped"], 2)
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_stage_b_backfill_idempotency_and_partial_failure_retry(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_stage_b_")
        try:
            manager = _create_sqlite_manager(temp_dir, "stage_b_test")
            fid = manager.get_or_create_float(1900121, "PROJ", "APEX")
            for cyc in (44, 45):
                with manager.mysql_engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO argo_profiles (
                            float_id, cycle_number, direction, profile_time,
                            latitude, longitude, pressure, temperature, salinity
                        ) VALUES (
                            :fid, :cyc, 'A', :pt, -10.5, 48.9,
                            '[7.8, 8.9]', '[25.0, 24.5]', '[35.1, 35.2]'
                        )
                    """), {"fid": fid, "cyc": cyc, "pt": f"2004-01-1{cyc - 40} 09:45:18"})

            # Seed 2 legacy vectors
            for pid, cyc in ((1, 44), (2, 45)):
                manager.chroma_collection.add(
                    ids=[f"profile_{pid}"],
                    embeddings=[[0.9, 0.9, 0.9]],
                    documents=[f"Legacy profile {pid}"],
                    metadatas=[{
                        "profile_id_sql": pid,
                        "float_id_sql": 1900121,
                        "cycle_number": cyc,
                        "date": "2004-01-15",
                        "latitude": -10.5,
                        "longitude": 48.9,
                    }],
                )

            # Inject failure on profile_id 2 during first Stage B run
            real_sync = manager.sync_profile_vector

            def flaky_sync(pid):
                if pid == 2:
                    raise RuntimeError("simulated chroma update failure")
                return real_sync(pid)

            manager.sync_profile_vector = flaky_sync
            res1 = promote.run_stage_b_backfill(manager)
            self.assertEqual(res1["updated"], 1)
            self.assertEqual(res1["failed"], 1)
            self.assertEqual(res1["failures"][0]["profile_id"], 2)

            # Retry with failure cleared: profile 1 is unchanged, profile 2 is updated
            manager.sync_profile_vector = real_sync
            res2 = promote.run_stage_b_backfill(manager)
            self.assertEqual(res2["unchanged"], 1)
            self.assertEqual(res2["updated"], 1)
            self.assertEqual(res2["failed"], 0)

            # Third run is completely idempotent with zero re-embedding
            embeds_before = manager.embedding_model.encode_calls
            res3 = promote.run_stage_b_backfill(manager)
            self.assertEqual(res3["unchanged"], 2)
            self.assertEqual(res3["updated"], 0)
            self.assertEqual(res3["failed"], 0)
            self.assertEqual(manager.embedding_model.encode_calls, embeds_before)
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_vector_metadata_contract_helper_passes(self):
        contract = promote.verify_vector_metadata_contract()
        self.assertTrue(contract["ok"])

    @unittest.skipUnless(
        CORPUS_EXCEPTIONS_AVAILABLE,
        "BGC NetCDF files for 6990679_001 and 7902200_015 not available",
    )
    def test_exact_rollback_deletes_only_manifest_owned_records(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_rb_exact_")
        manifest_path = os.path.join(temp_dir, "stage_a_manifest.json")
        try:
            manager = _create_sqlite_manager(temp_dir, "argo_float_profiles")
            fid_existing = manager.get_or_create_float(1900121, "EXISTING", "APEX")
            with manager.mysql_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO argo_profiles (
                        float_id, cycle_number, direction, profile_time,
                        latitude, longitude, pressure, temperature, salinity
                    ) VALUES (
                        :fid, 44, 'A', '2004-01-15 09:45:18', -10.5, 48.9,
                        '[7.8]', '[25.0]', '[35.1]'
                    )
                """), {"fid": fid_existing})
            manager.add_profile_from_row(1)

            manifest = promote.run_stage_a_ingest(
                manager,
                list(EXCEPTION_FILES),
                expected_total_profiles=3,
                manifest_path=manifest_path,
                target_db="argo_ocean_data_fresh",
                target_collection="argo_float_profiles",
            )
            self.assertEqual(manifest["promoted_wmos"], [6990679, 7902200])
            self.assertEqual(len(manifest["inserted_floats"]), 2)
            self.assertEqual(len(manifest["inserted_profiles"]), 2)
            self.assertEqual(manager.chroma_collection.count(), 3)

            # Dry-run rollback mutates nothing
            dry_rb = promote.rollback_stage_a(
                manager, manifest_path=manifest_path, dry_run=True
            )
            self.assertTrue(dry_rb["ownership_verified"])
            self.assertEqual(dry_rb["profiles_to_delete"], 2)
            self.assertEqual(dry_rb["floats_to_delete"], 2)
            self.assertEqual(manager.chroma_collection.count(), 3)

            # Applied rollback removes only promoted rows, floats, and vectors
            rb = promote.rollback_stage_a(
                manager, manifest_path=manifest_path, dry_run=False
            )
            self.assertEqual(rb["profiles_to_delete"], 2)
            self.assertEqual(rb["floats_to_delete"], 2)
            self.assertEqual(rb["vectors_to_delete"], 2)
            with manager.mysql_engine.connect() as conn:
                rem_profiles = conn.execute(text("SELECT profile_id FROM argo_profiles")).fetchall()
                rem_floats = conn.execute(text("SELECT wmo_number FROM argo_floats")).fetchall()
            self.assertEqual([int(r[0]) for r in rem_profiles], [1])
            self.assertEqual([int(r[0]) for r in rem_floats], [1900121])
            self.assertEqual(manager.chroma_collection.get(include=[])["ids"], ["profile_1"])
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipUnless(
        CORPUS_EXCEPTIONS_AVAILABLE,
        "BGC NetCDF files for 6990679_001 and 7902200_015 not available",
    )
    def test_rollback_refuses_unexpected_records_and_ownership_mismatch(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_rb_refuse_")
        manifest_path = os.path.join(temp_dir, "stage_a_manifest.json")
        try:
            manager = _create_sqlite_manager(temp_dir, "argo_float_profiles")
            manifest = promote.run_stage_a_ingest(
                manager,
                list(EXCEPTION_FILES),
                expected_total_profiles=2,
                manifest_path=manifest_path,
                target_db="argo_ocean_data_fresh",
                target_collection="argo_float_profiles",
            )
            promoted_fid = manifest["inserted_floats"][0]["float_id"]

            # 1. Unexpected child profile on a promoted float -> refuse
            with manager.mysql_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO argo_profiles (
                        float_id, cycle_number, direction, profile_time,
                        latitude, longitude, pressure, temperature, salinity
                    ) VALUES (
                        :fid, 999, 'A', '2025-01-01 00:00:00', 0.0, 0.0,
                        '[1.0]', '[20.0]', '[35.0]'
                    )
                """), {"fid": promoted_fid})
            with self.assertRaises(RuntimeError) as ctx:
                promote.rollback_stage_a(manager, manifest_path=manifest_path, dry_run=False)
            self.assertIn("Unexpected profile", str(ctx.exception))
            self.assertEqual(manager.chroma_collection.count(), 2)

            # Remove unexpected child and instead tamper with a manifest entry -> ownership mismatch
            with manager.mysql_engine.begin() as conn:
                conn.execute(text("DELETE FROM argo_profiles WHERE cycle_number = 999"))
            manifest["inserted_profiles"][0]["cycle_number"] = 888
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaises(RuntimeError) as ctx2:
                promote.rollback_stage_a(manager, manifest_path=manifest_path, dry_run=False)
            self.assertIn("Ownership mismatch", str(ctx2.exception))
            self.assertEqual(manager.chroma_collection.count(), 2)
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_rollback_preserves_concurrently_allocated_interleaved_ids(self):
        temp_dir = tempfile.mkdtemp(prefix="bgc_rb_conc_")
        manifest_path = os.path.join(temp_dir, "stage_a_manifest.json")
        try:
            manager = _create_sqlite_manager(temp_dir, "argo_float_profiles")
            fid_existing = manager.get_or_create_float(1900121, "EXISTING", "APEX")
            fid_p1 = manager.get_or_create_float(6990679, "BGC", "ARVOR")
            fid_p2 = manager.get_or_create_float(7902200, "BGC", "ARVOR")

            # Interleaved IDs: profile 1 (promoted), profile 2 (concurrent unrelated), profile 3 (promoted)
            rows = [
                (fid_p1, 1, "2024-12-01 13:28:00"),
                (fid_existing, 50, "2024-12-05 00:00:00"),
                (fid_p2, 15, "2024-12-26 13:10:58"),
            ]
            for idx, (fid, cyc, pt) in enumerate(rows, start=1):
                with manager.mysql_engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO argo_profiles (
                            float_id, cycle_number, direction, profile_time,
                            latitude, longitude, pressure, temperature, salinity
                        ) VALUES (
                            :fid, :cyc, 'A', :pt, 1.0, 2.0, '[1.0]', '[20.0]', '[35.0]'
                        )
                    """), {"fid": fid, "cyc": cyc, "pt": pt})
                manager.add_profile_from_row(idx)

            manifest = {
                "stage": "ingest",
                "target_database": "argo_ocean_data_fresh",
                "target_chroma_collection": "argo_float_profiles",
                "promoted_wmos": [6990679, 7902200],
                "inserted_floats": [
                    {"wmo_number": 6990679, "float_id": fid_p1},
                    {"wmo_number": 7902200, "float_id": fid_p2},
                ],
                "inserted_profiles": [
                    {"profile_id": 1, "float_id": fid_p1, "wmo_number": 6990679, "cycle_number": 1, "direction": "A", "profile_time": "2024-12-01 13:28:00"},
                    {"profile_id": 3, "float_id": fid_p2, "wmo_number": 7902200, "cycle_number": 15, "direction": "A", "profile_time": "2024-12-26 13:10:58"},
                ],
                "inserted_vector_ids": ["profile_1", "profile_3"],
            }
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)

            rb = promote.rollback_stage_a(manager, manifest_path=manifest_path, dry_run=False)
            self.assertEqual(rb["profiles_to_delete"], 2)
            self.assertEqual(rb["floats_to_delete"], 2)
            with manager.mysql_engine.connect() as conn:
                rem_pids = [int(r[0]) for r in conn.execute(text("SELECT profile_id FROM argo_profiles")).fetchall()]
                rem_wmos = [int(r[0]) for r in conn.execute(text("SELECT wmo_number FROM argo_floats")).fetchall()]
            self.assertEqual(rem_pids, [2])
            self.assertEqual(rem_wmos, [1900121])
            self.assertEqual(manager.chroma_collection.get(include=[])["ids"], ["profile_2"])
        finally:
            manager.chroma_client = None
            manager.mysql_engine.dispose()
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
