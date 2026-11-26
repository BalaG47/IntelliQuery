"""ChromaDB metadata for core-only and core+BGC profiles.

Covers the metadata added for BGC hardening — ``has_bgc``, ``product_type``,
``bgc_parameters`` and ``bgc_parameter_count`` — the document text that must
keep naming the BGC parameters, and filtering a real (temporary) ChromaDB
collection by the new keys. No MySQL is touched.
"""

import shutil
import tempfile
import unittest
from datetime import datetime

import chromadb

from database_manager import DatabaseManager

TIME = datetime(2024, 5, 6, 7, 8, 9)
LEGACY_KEYS = {"profile_id_sql", "float_id_sql", "cycle_number", "date"}
SCALAR_TYPES = (bool, int, float, str)


class MetadataSchemaTests(unittest.TestCase):
    """The metadata of one profile, built without any database connection."""

    def setUp(self):
        # No MySQL, embedding model or ChromaDB client is needed to build it.
        self.manager = object.__new__(DatabaseManager)

    def metadata(self, bgc_keys=None, lat=12.5, lon=-45.0, direction="A"):
        return self.manager._build_profile_metadata(
            profile_id_db=7,
            float_id_db=3,
            cycle=12,
            time=TIME,
            lat=lat,
            lon=lon,
            bgc_keys=bgc_keys,
            direction=direction,
        )

    def document(self, bgc_keys=None):
        return self.manager._create_profile_document(
            profile_id_db=7,
            float_id_db=3,
            cycle=12,
            time=TIME,
            lat=12.5,
            lon=-45.0,
            pressure=[1.0, 2.0],
            temperature=[20.0, 19.0],
            salinity=[35.0, 35.1],
            direction="A",
            bgc_keys=bgc_keys,
        )

    def test_core_only_profile_metadata(self):
        metadata = self.metadata()

        self.assertIs(metadata["has_bgc"], False)
        self.assertEqual(metadata["product_type"], "core")
        self.assertEqual(metadata["bgc_parameters"], "")
        self.assertEqual(metadata["bgc_parameter_count"], 0)

    def test_core_bgc_profile_metadata(self):
        metadata = self.metadata(["DOXY", "CHLA", "BBP700"])

        self.assertIs(metadata["has_bgc"], True)
        self.assertEqual(metadata["product_type"], "core_bgc")
        self.assertEqual(metadata["bgc_parameters"], "BBP700,CHLA,DOXY")
        self.assertEqual(metadata["bgc_parameter_count"], 3)

    def test_existing_metadata_fields_are_intact(self):
        metadata = self.metadata(["DOXY"])

        self.assertTrue(LEGACY_KEYS <= set(metadata))
        self.assertEqual(metadata["profile_id_sql"], 7)
        self.assertEqual(metadata["float_id_sql"], 3)
        self.assertEqual(metadata["cycle_number"], 12)
        self.assertEqual(metadata["date"], "2024-05-06")
        self.assertEqual(metadata["latitude"], 12.5)
        self.assertEqual(metadata["longitude"], -45.0)
        self.assertEqual(metadata["direction"], "A")

    def test_different_parameter_combinations(self):
        cases = {
            ("DOXY",): ("DOXY", 1),
            ("NITRATE", "DOXY"): ("DOXY,NITRATE", 2),
            ("PH_IN_SITU_TOTAL", "CHLA", "DOXY", "BBP700"):
                ("BBP700,CHLA,DOXY,PH_IN_SITU_TOTAL", 4),
            ("DOXY", "CHLA", "BBP700", "NITRATE", "PH_IN_SITU_TOTAL"):
                ("BBP700,CHLA,DOXY,NITRATE,PH_IN_SITU_TOTAL", 5),
        }
        for keys, (expected, count) in cases.items():
            with self.subTest(keys=keys):
                metadata = self.metadata(list(keys))
                self.assertEqual(metadata["bgc_parameters"], expected)
                self.assertEqual(metadata["bgc_parameter_count"], count)
                self.assertIs(metadata["has_bgc"], True)

    def test_parameter_order_and_duplicates_do_not_change_metadata(self):
        first = self.metadata(["DOXY", "CHLA", "DOXY"])
        second = self.metadata(["CHLA", "DOXY"])

        self.assertEqual(first["bgc_parameters"], "CHLA,DOXY")
        self.assertEqual(first["bgc_parameters"], second["bgc_parameters"])
        self.assertEqual(first, second)

    def test_dict_of_parameters_is_accepted(self):
        # The reconciliation path passes the stored bgc_params mapping itself.
        metadata = self.metadata({"DOXY": [1.0], "CHLA": [2.0]})

        self.assertEqual(metadata["bgc_parameters"], "CHLA,DOXY")
        self.assertEqual(metadata["bgc_parameters"],
                         self.metadata(["DOXY", "CHLA"])["bgc_parameters"])

    def test_metadata_values_are_chroma_scalars(self):
        metadata = self.metadata(["DOXY", "NITRATE"])

        for key, value in metadata.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, SCALAR_TYPES)

    def test_missing_position_and_unknown_direction_are_omitted(self):
        metadata = self.metadata(["DOXY"], lat=None, lon=None, direction="-")

        self.assertNotIn("latitude", metadata)
        self.assertNotIn("longitude", metadata)
        self.assertNotIn("direction", metadata)
        self.assertIs(metadata["has_bgc"], True)

    def test_core_document_still_says_no_bgc_parameters(self):
        document = self.document()

        self.assertIn("Bio-geochemical Parameters", document)
        self.assertIn("none", document)

    def test_bgc_document_names_every_parameter(self):
        document = self.document(["DOXY", "CHLA", "NITRATE"])

        for name in ("DOXY", "CHLA", "NITRATE"):
            self.assertIn(name, document)


class ChromaFilterTests(unittest.TestCase):
    """The new keys are usable in real ``where`` filters."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="bgc_metadata_filter_")
        client = chromadb.PersistentClient(path=cls.directory)
        cls.collection = client.get_or_create_collection(
            name="bgc_metadata_filter_test",
            metadata={"hnsw:space": "cosine"},
        )

        manager = object.__new__(DatabaseManager)
        profiles = [
            (1, None),
            (2, ["DOXY", "CHLA", "BBP700"]),
            (3, ["CHLA", "DOXY", "NITRATE", "PH_IN_SITU_TOTAL"]),
        ]
        ids, metadatas, documents, embeddings = [], [], [], []
        for profile_id, bgc_keys in profiles:
            ids.append(f"profile_{profile_id}")
            metadatas.append(manager._build_profile_metadata(
                profile_id_db=profile_id,
                float_id_db=1,
                cycle=profile_id,
                time=TIME,
                lat=10.0 + profile_id,
                lon=-20.0,
                bgc_keys=bgc_keys,
                direction="A",
            ))
            documents.append(manager._create_profile_document(
                profile_id_db=profile_id,
                float_id_db=1,
                cycle=profile_id,
                time=TIME,
                lat=10.0 + profile_id,
                lon=-20.0,
                pressure=[1.0, 2.0],
                temperature=[20.0, 19.0],
                salinity=[35.0, 35.1],
                direction="A",
                bgc_keys=bgc_keys,
            ))
            embeddings.append([0.1, 0.2, 0.3])

        cls.collection.add(ids=ids, metadatas=metadatas,
                           documents=documents, embeddings=embeddings)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def ids(self, **where):
        return set(self.collection.get(where=where)["ids"])

    def test_all_three_vectors_were_stored(self):
        self.assertEqual(self.collection.count(), 3)

    def test_filter_bgc_profiles(self):
        self.assertEqual(self.ids(has_bgc=True),
                         {"profile_2", "profile_3"})

    def test_filter_core_only_profiles(self):
        self.assertEqual(self.ids(has_bgc=False), {"profile_1"})

    def test_filter_by_product_type(self):
        self.assertEqual(self.ids(product_type="core"), {"profile_1"})
        self.assertEqual(self.ids(product_type="core_bgc"),
                         {"profile_2", "profile_3"})

    def test_filter_by_exact_parameter_list(self):
        self.assertEqual(self.ids(bgc_parameters="BBP700,CHLA,DOXY"),
                         {"profile_2"})

    def test_filter_by_parameter_count(self):
        self.assertEqual(
            set(self.collection.get(
                where={"bgc_parameter_count": {"$gte": 3}})["ids"]),
            {"profile_2", "profile_3"},
        )
        self.assertEqual(
            set(self.collection.get(
                where={"bgc_parameter_count": {"$eq": 4}})["ids"]),
            {"profile_3"},
        )

    def test_metadata_is_consistent_with_the_document(self):
        result = self.collection.get(
            where={"has_bgc": True}, include=["metadatas", "documents"])
        for metadata, document in zip(result["metadatas"],
                                      result["documents"]):
            for name in metadata["bgc_parameters"].split(","):
                self.assertIn(name, document)

    def test_legacy_fields_survive_for_every_record(self):
        metadatas = self.collection.get(include=["metadatas"])["metadatas"]
        for metadata in metadatas:
            self.assertTrue(LEGACY_KEYS <= set(metadata))
            self.assertIn("has_bgc", metadata)
            self.assertIn("product_type", metadata)


if __name__ == "__main__":
    unittest.main()
