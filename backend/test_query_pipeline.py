"""Phase 3C-E end-to-end pipeline and live-MySQL compatibility tests.

The fake-engine tests are deterministic and require no database.  The live
tests are read-only and are skipped unless INTELLIQUERY_LIVE_MYSQL=1 is set.
"""

import os
import unittest

from query_pipeline import PipelineStatus, run_pipeline
from query_planner import ResultShape, plan_query
from query_spec import (
    Aggregation,
    GroupBy,
    Intent,
    MeasurementValues,
    Parameter,
    QuerySpec,
    SortDirection,
    SortField,
    SortSpec,
)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self._engine.calls.append((str(statement), dict(params or {})))
        if self._engine.failure:
            raise RuntimeError("simulated database failure")
        return _Result(self._engine.rows)


class FakeEngine:
    def __init__(self, rows=(), failure=False):
        self.rows = list(rows)
        self.failure = failure
        self.calls = []

    def connect(self):
        return _Connection(self)


class ExplodingEngine:
    def __init__(self):
        self.connected = False

    def connect(self):
        self.connected = True
        raise AssertionError("no database connection was expected")


class PipelineStatusTests(unittest.TestCase):
    def test_valid_natural_language_query_executes(self):
        engine = FakeEngine(
            [
                {
                    "profile_id": 1,
                    "wmo_number": 1900121,
                    "cycle_number": 44,
                    "profile_time": "2020-01-01 00:00:00",
                    "latitude": 1.0,
                    "longitude": 65.0,
                    "data_mode": "D",
                    "level_count": 44,
                }
            ]
        )
        result = run_pipeline("List profiles for WMO 1900121", engine)

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertIsNotNone(result.plan)
        self.assertTrue(result.plan.supported)
        self.assertEqual(result.plan.template_id, "list_profiles")
        self.assertEqual(result.result_shape, "profile_rows")
        self.assertEqual(result.rows[0]["wmo_number"], 1900121)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0][1]["wmo_0"], 1900121)

    def test_count_question_executes_count_shape(self):
        engine = FakeEngine([{"profile_count": 3}])
        result = run_pipeline("How many profiles for WMO 1900121?", engine)

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertEqual(result.result_shape, "count")
        self.assertEqual(result.rows, ({"profile_count": 3},))

    def test_profile_detail_question_executes(self):
        engine = FakeEngine([{"profile_id": 1, "wmo_number": 1900121}])
        result = run_pipeline("Show temperature for WMO 1900121", engine)

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertEqual(result.plan.template_id, "profile_detail")
        self.assertEqual(result.result_shape, "profile_detail")
        self.assertEqual(len(engine.calls), 1)

    def test_empty_result_is_executed_not_unsupported(self):
        engine = FakeEngine([])
        result = run_pipeline("List profiles for WMO 1900121", engine)

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertEqual(result.rows, ())
        self.assertIsNone(result.error)

    def test_conversational_has_no_database_query(self):
        engine = ExplodingEngine()
        result = run_pipeline("Hello", engine)

        self.assertIs(result.status, PipelineStatus.NO_DATABASE_QUERY)
        self.assertFalse(engine.connected)
        self.assertEqual(result.rows, ())


class SafeStopTests(unittest.TestCase):
    def test_empty_question_is_invalid_without_sql(self):
        engine = ExplodingEngine()
        result = run_pipeline("   ", engine)

        self.assertIs(result.status, PipelineStatus.INVALID)
        self.assertIsNone(result.plan)
        self.assertFalse(engine.connected)

    def test_depth_clarification_stops_before_planning(self):
        engine = ExplodingEngine()
        result = run_pipeline(
            "Show temperature at 500 metres depth", engine
        )

        self.assertIs(result.status, PipelineStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(result.plan)
        self.assertIn("pressure", result.message.lower())
        self.assertIn("dbar", result.message.lower())
        self.assertFalse(engine.connected)

    def test_pressure_filter_is_unsupported_and_not_executed(self):
        engine = ExplodingEngine()
        result = run_pipeline(
            "Show pressure between 0 and 500 dbar", engine
        )

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertIsNotNone(result.plan)
        self.assertFalse(result.plan.supported)
        self.assertIn("Pressure filtering", result.message)
        self.assertFalse(engine.connected)

    def test_measurement_qc_is_unsupported_and_not_executed(self):
        result = run_pipeline(
            "Show good-quality temperature measurements", ExplodingEngine()
        )

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertIn("Measurement-level QC", result.message)

    def test_mean_is_unsupported_and_not_executed(self):
        result = run_pipeline("Show mean temperature", ExplodingEngine())

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertIn("MEAN", result.message)

    def test_min_and_max_are_unsupported(self):
        result = run_pipeline(
            "Show minimum and maximum temperature", ExplodingEngine()
        )

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)

    def test_compare_is_unsupported(self):
        result = run_pipeline(
            "Compare temperature for WMO 1900121 and WMO 1900122",
            ExplodingEngine(),
        )

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertIn("Comparison", result.message)

    def test_semantic_discovery_is_unsupported_by_sql_pipeline(self):
        result = run_pipeline("Tell me about the ocean", ExplodingEngine())

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertIn("ChromaDB", result.message)

    def test_incomplete_pressure_limitation_is_not_executed(self):
        # Phase 3C-C known limitation: these parse as VALID but carry no
        # pressure filter.  The integration guard must refuse rather than
        # execute the incomplete interpretation.
        for question in (
            "Show pressure between 0",
            "Show pressure from 0 dbar",
        ):
            with self.subTest(question=question):
                engine = ExplodingEngine()
                result = run_pipeline(question, engine)
                self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
                self.assertIn("incomplete", result.message.lower())
                self.assertFalse(engine.connected)

    def test_unsupported_filter_plus_valid_wmo_does_not_execute(self):
        engine = ExplodingEngine()
        result = run_pipeline(
            "Show pressure between 0 and 500 dbar for WMO 1900121", engine
        )

        self.assertIs(result.status, PipelineStatus.UNSUPPORTED)
        self.assertFalse(engine.connected)

    def test_missing_engine_for_sql_is_structured_error(self):
        result = run_pipeline("List profiles for WMO 1900121", None)

        self.assertIs(result.status, PipelineStatus.EXECUTION_ERROR)
        self.assertIsNotNone(result.plan)
        self.assertIsNotNone(result.plan.sql)
        self.assertEqual(result.rows, ())
        self.assertIn("No database engine", result.error)

    def test_database_failure_is_structured_error(self):
        result = run_pipeline(
            "List profiles for WMO 1900121", FakeEngine(failure=True)
        )

        self.assertIs(result.status, PipelineStatus.EXECUTION_ERROR)
        self.assertEqual(result.rows, ())
        self.assertEqual(
            result.error, "Query execution failed; no result was produced."
        )
        self.assertNotIn("simulated database failure", result.error)


class ScientificIntegrityTests(unittest.TestCase):
    def test_null_measurements_are_preserved(self):
        engine = FakeEngine([{"profile_id": 1, "temperature": None}])
        result = run_pipeline("Show temperature for WMO 1900121", engine)

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertIsNone(result.rows[0]["temperature"])

    def test_adjusted_question_selects_adjusted_columns(self):
        result = run_pipeline(
            "Show adjusted temperature for WMO 1900121", FakeEngine([])
        )
        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertIn("temperature_adjusted", result.plan.sql)
        self.assertIn("temperature_adjusted_qc", result.plan.sql)
        self.assertNotIn("p.temperature,", result.plan.sql)

    def test_detail_keeps_profile_and_parameter_data_mode_distinct(self):
        result = run_pipeline(
            "Show temperature for WMO 1900121", FakeEngine([])
        )
        self.assertIn("p.data_mode", result.plan.sql)
        self.assertIn("p.parameter_data_mode", result.plan.sql)

    def test_detail_keeps_provenance(self):
        result = run_pipeline(
            "Show temperature for WMO 1900121", FakeEngine([])
        )
        self.assertIn("p.source_file", result.plan.sql)
        self.assertIn("p.source_file_sha256", result.plan.sql)

    def test_bgc_availability_reads_stored_json_not_a_column(self):
        result = run_pipeline(
            "Which floats have chlorophyll measurements?", FakeEngine([])
        )

        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertIn("$.CHLA", result.plan.sql)
        self.assertIn("p.bgc_params", result.plan.sql)
        self.assertNotIn("p.chla", result.plan.sql.lower())
        self.assertEqual(result.rows, ())

    def test_no_depth_column_is_ever_referenced(self):
        result = run_pipeline("List profiles for WMO 1900121", FakeEngine([]))
        self.assertNotIn("depth", result.plan.sql.lower())

    def test_planner_rejects_pressure_before_any_execution(self):
        spec = QuerySpec(
            Intent.LIST_PROFILES,
            parameters=(Parameter.PRES,),
            pressure_range=(0.0, 500.0),
        )
        plan = plan_query(spec)
        self.assertFalse(plan.supported)
        self.assertIsNone(plan.sql)
        self.assertIs(plan.result_shape, ResultShape.NONE)


@unittest.skipUnless(
    os.environ.get("INTELLIQUERY_LIVE_MYSQL") == "1",
    "set INTELLIQUERY_LIVE_MYSQL=1 for read-only MySQL compatibility tests",
)
class LiveMySQLCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import URL

        from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

        cls.engine = create_engine(
            URL.create(
                "mysql+mysqlconnector",
                username=DB_USER,
                password=DB_PASSWORD,
                host=DB_HOST,
                port=int(DB_PORT),
                database=DB_NAME,
            )
        )

        with cls.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT f.wmo_number, p.cycle_number "
                    "FROM argo_profiles p "
                    "JOIN argo_floats f ON p.float_id = f.float_id "
                    "WHERE p.pressure IS NOT NULL "
                    "ORDER BY p.profile_id LIMIT 1"
                )
            ).mappings().fetchone()

        if row is None:
            raise unittest.SkipTest("database has no profile with measurements")
        cls.wmo_number = int(row["wmo_number"])
        cls.cycle_number = int(row["cycle_number"])

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def execute(self, spec):
        from query_planner import execute_spec

        return execute_spec(spec, self.engine)

    def test_float_enumeration_template(self):
        result = self.execute(
            QuerySpec(Intent.ENUMERATE_FLOATS, limit=2)
        )
        self.assertEqual(result["result_shape"], "float_rows")
        self.assertLessEqual(len(result["rows"]), 2)
        if result["rows"]:
            self.assertIn("wmo_number", result["rows"][0])

    def test_count_template(self):
        result = self.execute(
            QuerySpec(
                Intent.COUNT_PROFILES,
                wmo_numbers=(self.wmo_number,),
            )
        )
        self.assertEqual(result["result_shape"], "count")
        self.assertGreaterEqual(result["rows"][0]["profile_count"], 1)

    def test_grouped_count_template(self):
        result = self.execute(
            QuerySpec(
                Intent.AGGREGATE,
                aggregations=(Aggregation.COUNT,),
                group_by=None,
            )
        )
        self.assertEqual(result["result_shape"], "count")

        grouped = self.execute(
            QuerySpec(
                Intent.AGGREGATE,
                aggregations=(Aggregation.COUNT,),
                group_by=GroupBy.YEAR,
                limit=3,
            )
        )
        self.assertEqual(grouped["result_shape"], "count_by_group")
        self.assertLessEqual(len(grouped["rows"]), 3)
        if grouped["rows"]:
            self.assertIn("group_key", grouped["rows"][0])
            self.assertIn("profile_count", grouped["rows"][0])

    def test_profile_list_template_bound_limit_and_sort(self):
        result = self.execute(
            QuerySpec(
                Intent.LIST_PROFILES,
                wmo_numbers=(self.wmo_number,),
                sort=SortSpec(SortField.CYCLE_NUMBER, SortDirection.ASC),
                limit=2,
            )
        )
        self.assertEqual(result["result_shape"], "profile_rows")
        self.assertLessEqual(len(result["rows"]), 2)
        if result["rows"]:
            self.assertIn("profile_time", result["rows"][0])
            self.assertIn("level_count", result["rows"][0])

    def test_profile_detail_template(self):
        result = self.execute(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(self.wmo_number,),
                cycle_numbers=(self.cycle_number,),
            )
        )
        self.assertEqual(result["result_shape"], "profile_detail")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertIn("source_file", row)
        self.assertIn("source_file_sha256", row)
        self.assertIn("pressure", row)
        self.assertIn("parameter_data_mode", row)

    def test_parameter_availability_template(self):
        result = self.execute(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.TEMP,),
                wmo_numbers=(self.wmo_number,),
                limit=2,
            )
        )
        self.assertEqual(result["result_shape"], "availability_rows")
        self.assertLessEqual(len(result["rows"]), 2)
        if result["rows"]:
            self.assertIn("has_TEMP", result["rows"][0])

    def test_metadata_template(self):
        result = self.execute(
            QuerySpec(
                Intent.METADATA_INFO,
                wmo_numbers=(self.wmo_number,),
            )
        )
        self.assertEqual(result["result_shape"], "float_metadata")
        self.assertEqual(len(result["rows"]), 1)
        self.assertIn("profile_count", result["rows"][0])

    def test_pipeline_end_to_end_against_mysql(self):
        result = run_pipeline(
            f"List profiles for WMO {self.wmo_number}", self.engine
        )
        self.assertIs(result.status, PipelineStatus.EXECUTED)
        self.assertEqual(result.plan.template_id, "list_profiles")
        self.assertLessEqual(len(result.rows), 5)
        if result.rows:
            self.assertEqual(result.rows[0]["wmo_number"], self.wmo_number)

    def test_mysql_json_null_behavior(self):
        result = self.execute(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.DOXY,),
                limit=2,
            )
        )
        self.assertEqual(result["result_shape"], "availability_rows")
        for row in result["rows"]:
            self.assertIn("has_DOXY", row)


if __name__ == "__main__":
    unittest.main()
