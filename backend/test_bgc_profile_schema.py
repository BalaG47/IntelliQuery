"""Database-schema validation tests for profile ingestion (B5).

Unit level only: no MySQL connection is opened. ``DatabaseManager`` is built with
``__new__`` so its MySQL/ChromaDB/embedding constructor is skipped, and the
schema caches are set directly.

What is established:
  * a database without the V2 migration fails loudly, naming the missing
    columns, instead of silently dropping QC / adjusted / BGC data;
  * a failed schema inspection is raised, never mistaken for an older schema;
  * payload fields that the table does not have are rejected;
  * the session cannot insert profiles until the identity key is
    (float_id, cycle_number, profile_index_in_file) (B2).
"""

import unittest

from database_manager import DatabaseManager


class FailingEngine:
    """An engine whose connect() always fails, as a broken database would."""

    def connect(self):
        raise RuntimeError("connection refused")


class ProfileSchemaValidationTests(unittest.TestCase):
    def manager(self, columns=None, unique_keys=None):
        manager = DatabaseManager.__new__(DatabaseManager)
        manager._cached_profile_cols = (
            set(DatabaseManager.REQUIRED_PROFILE_COLUMNS)
            if columns is None else columns
        )
        if unique_keys is None:
            unique_keys = (DatabaseManager.PROFILE_IDENTITY_KEY,)
        manager._cached_profile_unique_keys = unique_keys
        return manager

    def payload(self, **overrides):
        base = {
            "float_id": 1,
            "cycle_number": 2,
            "profile_index_in_file": 0,
            "bgc_params": "{}",
        }
        base.update(overrides)
        return base

    def test_complete_migrated_schema_is_accepted(self):
        manager = self.manager()

        manager._validate_profile_schema(self.payload().keys())

    def test_every_v2_bgc_column_is_required(self):
        for column in ("bgc_params", "bgc_qc", "bgc_adjusted",
                       "bgc_adjusted_error", "bgc_adjusted_qc",
                       "parameter_data_mode", "scientific_calib",
                       "source_file", "source_file_sha256",
                       "profile_index_in_file", "data_mode"):
            with self.subTest(column=column):
                columns = set(DatabaseManager.REQUIRED_PROFILE_COLUMNS) - {column}
                manager = self.manager(columns=columns)

                with self.assertRaises(RuntimeError) as ctx:
                    manager._validate_profile_schema(self.payload().keys())

                message = str(ctx.exception)
                self.assertIn(column, message)
                self.assertIn("V2", message)

    def test_insert_profile_refuses_before_writing_to_a_partial_schema(self):
        columns = set(DatabaseManager.REQUIRED_PROFILE_COLUMNS) - {"bgc_adjusted"}
        manager = self.manager(columns=columns)
        # Any attempt to touch the database would raise this instead.
        manager.mysql_engine = FailingEngine()

        with self.assertRaises(RuntimeError) as ctx:
            manager.insert_profile(self.payload())

        self.assertIn("bgc_adjusted", str(ctx.exception))

    def test_unknown_payload_field_is_rejected(self):
        manager = self.manager()

        with self.assertRaises(RuntimeError) as ctx:
            manager._validate_profile_schema(
                self.payload(unexpected_field="x").keys())

        self.assertIn("unexpected_field", str(ctx.exception))

    def test_schema_inspection_failure_is_not_treated_as_an_old_schema(self):
        manager = DatabaseManager.__new__(DatabaseManager)
        manager._cached_profile_cols = None
        manager.mysql_engine = FailingEngine()

        with self.assertRaises(RuntimeError) as ctx:
            manager._get_profile_table_columns()

        message = str(ctx.exception)
        self.assertIn("Could not inspect", message)
        # It must not fall back to the nine base columns.
        self.assertNotIn("bgc_params", message)

    def test_index_inspection_failure_is_raised(self):
        manager = self.manager()
        # Forget the cached keys so the index inspection must run.
        del manager._cached_profile_unique_keys
        manager.mysql_engine = FailingEngine()

        with self.assertRaises(RuntimeError) as ctx:
            manager._validate_profile_schema(self.payload().keys())

        self.assertIn("indexes", str(ctx.exception))

    def test_missing_identity_key_is_rejected(self):
        manager = self.manager(unique_keys=(("float_id", "cycle_number"),))

        with self.assertRaises(RuntimeError) as ctx:
            manager._validate_profile_schema(self.payload().keys())

        self.assertIn("V4", str(ctx.exception))

    def test_narrower_unique_key_is_rejected_even_with_the_wider_one(self):
        manager = self.manager(unique_keys=(
            ("float_id", "cycle_number"),
            DatabaseManager.PROFILE_IDENTITY_KEY,
        ))

        with self.assertRaises(RuntimeError) as ctx:
            manager._validate_profile_schema(self.payload().keys())

        self.assertIn("narrower", str(ctx.exception))

    def test_identity_key_constant_matches_the_migration(self):
        self.assertEqual(
            DatabaseManager.PROFILE_IDENTITY_KEY,
            ("float_id", "cycle_number", "direction", "profile_time"),
        )

    def test_the_pre_v4_key_is_rejected_as_narrower(self):
        manager = self.manager(unique_keys=(
            ("float_id", "cycle_number", "profile_index_in_file"),
            DatabaseManager.PROFILE_IDENTITY_KEY,
        ))

        with self.assertRaises(RuntimeError) as ctx:
            manager._validate_profile_schema(self.payload().keys())

        self.assertIn("V4", str(ctx.exception))

    def test_the_bgc_axis_and_provenance_columns_are_required(self):
        for column in ("direction", "bgc_pressure", "bgc_source_metadata"):
            with self.subTest(column=column):
                columns = set(DatabaseManager.REQUIRED_PROFILE_COLUMNS) - {column}
                manager = self.manager(columns=columns)

                with self.assertRaises(RuntimeError) as ctx:
                    manager._validate_profile_schema(self.payload().keys())

                self.assertIn(column, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
