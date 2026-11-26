"""Recovery of a committed MySQL profile whose ChromaDB vector is missing.

The failure window is injected deterministically: the fake collection raises
exactly when the vector write is attempted, so the relational row survives
while no vector exists. Re-running the ingestion path must rebuild the vector
from that row, without duplicating the row or the vector, and without changing
an existing, matching vector.
"""

import unittest
from datetime import datetime

from data_processor import ArgoDataProcessor
from database_manager import DatabaseManager

TIME = datetime(2024, 5, 6, 7, 8, 9)


class InjectedFailure(RuntimeError):
    """The controlled failure of a vector write."""


class _Vector(list):
    """Mirrors the ``.tolist()`` of a sentence-transformers embedding."""

    def tolist(self):
        return list(self)


class CountingEmbeddingModel:
    """Returns a short vector and records how often it was asked to embed."""

    def __init__(self):
        self.documents = []

    def encode(self, document):
        self.documents.append(document)
        return _Vector([0.1, 0.2, 0.3])


class FakeCollection:
    """In-memory stand-in for a ChromaDB collection.

    ``get``/``add``/``update`` mirror the production call shapes used by
    ``DatabaseManager.sync_profile_vector``. ``fail_next_add`` and
    ``fail_next_get`` raise once, which makes the failure point deterministic
    instead of time-based.
    """

    def __init__(self):
        self.records = {}
        self.add_calls = 0
        self.update_calls = 0
        self.fail_next_add = None
        self.fail_next_get = None

    def get(self, ids=None, include=None):
        if self.fail_next_get is not None:
            error, self.fail_next_get = self.fail_next_get, None
            raise error
        wanted = list(ids or self.records)
        found = [item for item in wanted if item in self.records]
        return {
            "ids": found,
            "documents": [self.records[item]["document"] for item in found],
            "metadatas": [dict(self.records[item]["metadata"])
                          for item in found],
        }

    def add(self, ids, embeddings, documents, metadatas):
        if self.fail_next_add is not None:
            error, self.fail_next_add = self.fail_next_add, None
            raise error
        for index, item in enumerate(ids):
            if item in self.records:
                raise AssertionError(f"duplicate vector id added: {item}")
            self.records[item] = {
                "embedding": embeddings[index],
                "document": documents[index],
                "metadata": dict(metadatas[index]),
            }
        self.add_calls += 1

    def update(self, ids, embeddings, documents, metadatas):
        for index, item in enumerate(ids):
            if item not in self.records:
                raise AssertionError(f"update of unknown vector id: {item}")
            self.records[item] = {
                "embedding": embeddings[index],
                "document": documents[index],
                "metadata": dict(metadatas[index]),
            }
        self.update_calls += 1

    def count(self):
        return len(self.records)


def profile_record(profile_id=11, bgc_params=None):
    """The relational record a vector is rebuilt from (shape of a DB row)."""
    return {
        "profile_id": profile_id,
        "float_id": 3,
        "wmo_number": 6990679,
        "cycle_number": 12,
        "profile_time": TIME,
        "direction": "A",
        "latitude": 12.5,
        "longitude": -45.0,
        "pressure": [1.0, 2.0, 3.0],
        "temperature": [20.0, 19.0, 18.0],
        "salinity": [35.0, 35.1, 35.2],
        "bgc_params": (
            {"DOXY": [210.0, 205.0, 200.0]}
            if bgc_params is None else bgc_params
        ),
    }


def build_manager(record, collection):
    manager = object.__new__(DatabaseManager)
    manager.chroma_collection = collection
    manager.embedding_model = CountingEmbeddingModel()
    manager.get_profile_vector_record = lambda profile_id: (
        record if record is not None and profile_id == record["profile_id"]
        else None
    )
    return manager


class SyncProfileVectorTests(unittest.TestCase):
    """``sync_profile_vector`` rebuilds, updates or leaves a vector alone."""

    def setUp(self):
        self.record = profile_record()
        self.collection = FakeCollection()
        self.manager = build_manager(self.record, self.collection)

    def test_missing_vector_is_recreated_from_the_row(self):
        status = self.manager.sync_profile_vector(11)

        self.assertEqual(status, "recreated")
        self.assertEqual(self.collection.add_calls, 1)
        vector = self.collection.records["profile_11"]
        self.assertEqual(vector["metadata"]["profile_id_sql"], 11)
        self.assertIs(vector["metadata"]["has_bgc"], True)
        self.assertEqual(vector["metadata"]["product_type"], "core_bgc")
        self.assertEqual(vector["metadata"]["bgc_parameters"], "DOXY")
        self.assertEqual(vector["metadata"]["bgc_parameter_count"], 1)
        self.assertEqual(vector["metadata"]["latitude"], 12.5)
        self.assertEqual(vector["metadata"]["direction"], "A")
        self.assertIn("DOXY", vector["document"])
        self.assertEqual(len(vector["embedding"]), 3)

    def test_second_sync_leaves_the_vector_unchanged(self):
        self.manager.sync_profile_vector(11)
        embeddings_before = len(self.manager.embedding_model.documents)

        status = self.manager.sync_profile_vector(11)

        self.assertEqual(status, "unchanged")
        self.assertEqual(self.collection.add_calls, 1)
        self.assertEqual(self.collection.update_calls, 0)
        self.assertEqual(len(self.manager.embedding_model.documents),
                         embeddings_before)
        self.assertEqual(self.collection.count(), 1)

    def test_core_only_row_has_no_bgc_metadata(self):
        self.manager.get_profile_vector_record = lambda profile_id: {
            "profile_id": 12, "float_id": 3, "wmo_number": 6990679,
            "cycle_number": 13,
            "profile_time": TIME, "direction": "D", "latitude": None,
            "longitude": None, "pressure": [1.0], "temperature": [20.0],
            "salinity": [35.0], "bgc_params": {},
        }
        self.manager.sync_profile_vector(12)

        metadata = self.collection.records["profile_12"]["metadata"]
        self.assertIs(metadata["has_bgc"], False)
        self.assertEqual(metadata["product_type"], "core")
        self.assertEqual(metadata["bgc_parameters"], "")
        self.assertNotIn("latitude", metadata)
        self.assertNotIn("longitude", metadata)

    def test_stale_legacy_metadata_is_updated_not_duplicated(self):
        # A vector written before this change: same text, old metadata.
        _, document, _ = self.manager._build_profile_vector_payload(
            profile_id_db=11, float_id_db=3, cycle=12, time=TIME,
            lat=12.5, lon=-45.0, bgc_keys=["DOXY"],
            pressure=[1.0, 2.0, 3.0], temperature=[20.0, 19.0, 18.0],
            salinity=[35.0, 35.1, 35.2], direction="A")
        self.collection.records["profile_11"] = {
            "embedding": [0.0], "document": document,
            "metadata": {"profile_id_sql": 11, "float_id_sql": 3,
                         "cycle_number": 12, "date": "2024-05-06"},
        }

        status = self.manager.sync_profile_vector(11)

        self.assertEqual(status, "updated")
        self.assertEqual(self.collection.update_calls, 1)
        self.assertEqual(self.collection.add_calls, 0)
        self.assertEqual(self.collection.count(), 1)
        metadata = self.collection.records["profile_11"]["metadata"]
        self.assertIs(metadata["has_bgc"], True)
        self.assertEqual(metadata["bgc_parameters"], "DOXY")

    def test_stale_document_is_updated(self):
        self.manager.sync_profile_vector(11)
        self.collection.records["profile_11"]["document"] = "outdated text"

        status = self.manager.sync_profile_vector(11)

        self.assertEqual(status, "updated")
        self.assertEqual(self.collection.update_calls, 1)
        self.assertEqual(self.collection.count(), 1)

    def test_failed_vector_write_is_reported_and_retryable(self):
        self.collection.fail_next_add = InjectedFailure("vector store down")

        with self.assertRaises(InjectedFailure):
            self.manager.sync_profile_vector(11)
        self.assertEqual(self.collection.count(), 0)

        # The next attempt is the retry: it must succeed and add exactly once.
        self.assertEqual(self.manager.sync_profile_vector(11), "recreated")
        self.assertEqual(self.collection.count(), 1)
        self.assertEqual(self.collection.add_calls, 1)

    def test_vector_store_read_failure_is_reported(self):
        self.collection.fail_next_get = InjectedFailure("store unreadable")

        with self.assertRaises(InjectedFailure):
            self.manager.sync_profile_vector(11)

    def test_unknown_profile_cannot_be_rebuilt(self):
        manager = build_manager(None, self.collection)

        with self.assertRaises(ValueError):
            manager.sync_profile_vector(11)


class FakeDatabase:
    """Records the ingestion calls instead of touching MySQL or ChromaDB."""

    def __init__(self, existing_id=None, sync_error=None):
        self.existing_id = existing_id
        self.sync_error = sync_error
        self.inserted = []
        self.synced = []
        self.vectors_added = []

    def check_profile_exists(self, *identity):
        return self.existing_id is not None

    def get_profile_id(self, *identity):
        return self.existing_id

    def sync_profile_vector(self, profile_id):
        self.synced.append(profile_id)
        if self.sync_error is not None:
            raise self.sync_error
        return "recreated"

    def insert_profile(self, payload):
        self.inserted.append(payload)
        return 99

    def add_profile_to_chromadb(self, *args, **kwargs):
        self.vectors_added.append(args)

    def add_profile_from_row(self, profile_id):
        # The production manager reads the row back and calls
        # add_profile_to_chromadb; this fake records that same call.
        self.add_profile_to_chromadb(profile_id)


def payload():
    return {
        "float_id": 1,
        "cycle_number": 2,
        "direction": "A",
        "profile_time": TIME,
        "bgc_params": None,
        "pressure": None,
        "temperature": None,
        "salinity": None,
        "latitude": None,
        "longitude": None,
    }


class StorePayloadWiringTests(unittest.TestCase):
    """Ingestion reconciles the vector of a row it would otherwise skip."""

    def test_existing_row_repairs_its_vector_and_inserts_nothing(self):
        database = FakeDatabase(existing_id=42)
        processor = ArgoDataProcessor(database)

        status = processor._store_payload(payload(), "float")

        self.assertEqual(status, "skipped")
        self.assertEqual(database.inserted, [])
        self.assertEqual(database.vectors_added, [])
        self.assertEqual(database.synced, [42])

    def test_new_row_is_inserted_and_its_vector_written(self):
        database = FakeDatabase()
        processor = ArgoDataProcessor(database)

        status = processor._store_payload(payload(), "float")

        self.assertEqual(status, "inserted")
        self.assertEqual(len(database.inserted), 1)
        self.assertEqual(database.synced, [])
        self.assertEqual(len(database.vectors_added), 1)

    def test_reconciliation_failure_is_reported_not_swallowed(self):
        error = InjectedFailure("store down")
        database = FakeDatabase(existing_id=42, sync_error=error)
        processor = ArgoDataProcessor(database)

        with self.assertRaises(InjectedFailure):
            processor._store_payload(payload(), "float")
        self.assertEqual(database.inserted, [])

        # A later run retries the same reconciliation and succeeds. A new run
        # starts with an empty per-run identity set, as process_paths does.
        database.sync_error = None
        processor._identities_in_run.clear()
        self.assertEqual(
            processor._store_payload(payload(), "float"), "skipped")
        self.assertEqual(database.synced, [42, 42])
        self.assertEqual(database.inserted, [])

    def test_duplicate_identity_in_one_run_is_still_refused(self):
        database = FakeDatabase()
        processor = ArgoDataProcessor(database)
        processor._identities_in_run.add(
            (1, 2, "A", TIME))

        with self.assertRaises(RuntimeError):
            processor._store_payload(payload(), "float")


if __name__ == "__main__":
    unittest.main()
