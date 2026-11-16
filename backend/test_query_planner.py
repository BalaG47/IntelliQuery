"""Focused tests for the Phase 3C-D deterministic query planner.

No live MySQL connection is required: these tests inspect QueryPlan objects.
One small fake engine covers the separate execution boundary.
"""

import unittest
from datetime import datetime

from query_planner import (
    PlanOperation,
    ResultShape,
    execute_spec,
    plan_query,
    plan_validation,
)
from query_spec import (
    Aggregation,
    BoundingBox,
    DataMode,
    GroupBy,
    Intent,
    MeasurementValues,
    Parameter,
    QcFilter,
    QcScope,
    QuerySpec,
    SortDirection,
    SortField,
    SortSpec,
    TimeRange,
    ValidationResult,
    ValidationStatus,
    good_quality_filter,
    validate_spec,
)


class PlannerTestCase(unittest.TestCase):
    def plan(self, spec):
        return plan_query(spec)

    def assertUnsupported(self, plan):
        self.assertFalse(plan.supported)
        self.assertIsNone(plan.sql)
        self.assertEqual(plan.template_id, "unsupported")
        self.assertTrue(plan.reason)


class SupportedIntentTests(PlannerTestCase):
    def test_enumerate_floats_template_and_limit(self):
        plan = self.plan(
            QuerySpec(
                Intent.ENUMERATE_FLOATS,
                wmo_numbers=(1900001, 1900002),
                limit=2,
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.operation, PlanOperation.ENUMERATE_FLOATS)
        self.assertEqual(plan.template_id, "enumerate_floats")
        self.assertEqual(plan.result_shape, ResultShape.FLOAT_ROWS)
        self.assertIn("FROM argo_floats f", plan.sql)
        self.assertEqual(plan.params["wmo_0"], 1900001)
        self.assertEqual(plan.params["wmo_1"], 1900002)
        self.assertEqual(plan.limit, 2)
        self.assertIn("LIMIT :limit", plan.sql)

    def test_count_profiles_maps_all_supported_metadata_filters(self):
        plan = self.plan(
            QuerySpec(
                Intent.COUNT_PROFILES,
                parameters=(Parameter.TEMP,),
                wmo_numbers=(1900001,),
                cycle_numbers=(1, 2),
                time_range=TimeRange(
                    start=datetime(2020, 1, 1),
                    end=datetime(2020, 12, 31),
                ),
                bounding_box=BoundingBox(0.0, 10.0, 20.0, 30.0),
                data_mode=DataMode.DELAYED,
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.COUNT)
        self.assertIn("COUNT(*)", plan.sql)
        self.assertIn("f.wmo_number IN (:wmo_0)", plan.sql)
        self.assertIn("p.cycle_number IN (:cycle_0, :cycle_1)", plan.sql)
        self.assertIn("p.profile_time >= :start_time", plan.sql)
        self.assertIn("p.latitude <= :max_latitude", plan.sql)
        self.assertIn("p.data_mode = :data_mode", plan.sql)
        self.assertIn("JSON_LENGTH(COALESCE(p.temperature, '[]')) > 0", plan.sql)
        self.assertEqual(plan.params["data_mode"], "D")
        self.assertIsNone(plan.limit)
        self.assertEqual(plan.scan_limit, 500)

    def test_list_profiles_sort_and_limit(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                sort=SortSpec(SortField.LATITUDE, SortDirection.ASC),
                limit=3,
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.PROFILE_ROWS)
        self.assertIn("ORDER BY p.latitude ASC, p.profile_id ASC", plan.sql)
        self.assertEqual(plan.params["limit"], 3)
        self.assertEqual(plan.limit, 3)

    def test_list_profiles_default_limit(self):
        plan = self.plan(QuerySpec(Intent.LIST_PROFILES))
        self.assertTrue(plan.supported)
        self.assertEqual(plan.limit, 5)
        self.assertEqual(plan.params["limit"], 5)

    def test_profile_detail_requires_identifier_and_selects_metadata(self):
        plan = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
                cycle_numbers=(7,),
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.PROFILE_DETAIL)
        self.assertIn("p.profile_time", plan.sql)
        self.assertIn("p.source_file_sha256", plan.sql)
        self.assertIn("p.parameter_data_mode", plan.sql)
        self.assertEqual(plan.limit, 1)

    def test_profile_detail_without_identifier_is_refused(self):
        plan = self.plan(QuerySpec(Intent.PROFILE_DETAIL))
        self.assertUnsupported(plan)
        self.assertIn("WMO number or a cycle number", plan.reason)

    def test_parameter_availability_core_and_bgc(self):
        plan = self.plan(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.TEMP, Parameter.DOXY),
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.AVAILABILITY_ROWS)
        self.assertIn("AS has_TEMP", plan.sql)
        self.assertIn("AS has_DOXY", plan.sql)
        self.assertIn("$.DOXY", plan.sql)

    def test_metadata_info_uses_float_table(self):
        plan = self.plan(
            QuerySpec(Intent.METADATA_INFO, wmo_numbers=(1900001,))
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.FLOAT_METADATA)
        self.assertIn("FROM argo_floats f", plan.sql)
        self.assertIn("COUNT(p.profile_id) AS profile_count", plan.sql)

    def test_aggregate_count(self):
        plan = self.plan(
            QuerySpec(
                Intent.AGGREGATE,
                aggregations=(Aggregation.COUNT,),
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.COUNT)
        self.assertIn("COUNT(*)", plan.sql)

    def test_aggregate_count_grouped(self):
        plan = self.plan(
            QuerySpec(
                Intent.AGGREGATE,
                aggregations=(Aggregation.COUNT,),
                group_by=GroupBy.YEAR,
                limit=10,
            )
        )
        self.assertTrue(plan.supported)
        self.assertEqual(plan.result_shape, ResultShape.COUNT_BY_GROUP)
        self.assertIn("YEAR(p.profile_time) AS group_key", plan.sql)
        self.assertIn("GROUP BY YEAR(p.profile_time)", plan.sql)
        self.assertEqual(plan.params["limit"], 10)

    def test_conversational_has_no_sql(self):
        plan = self.plan(QuerySpec(Intent.CONVERSATIONAL))
        self.assertTrue(plan.supported)
        self.assertEqual(plan.operation, PlanOperation.NO_DATABASE_QUERY)
        self.assertIsNone(plan.sql)
        self.assertEqual(plan.result_shape, ResultShape.NONE)


class UnsupportedAndErrorTests(PlannerTestCase):
    def test_invalid_spec_is_not_plannable(self):
        plan = self.plan(
            QuerySpec(Intent.LIST_PROFILES, wmo_numbers=("1900001",))
        )
        self.assertUnsupported(plan)
        self.assertIn("WMO number must be an integer", plan.reason)

    def test_plan_validation_requires_validation_result(self):
        with self.assertRaises(TypeError):
            plan_validation(QuerySpec(Intent.LIST_PROFILES))

    def test_validation_result_passthrough(self):
        spec = QuerySpec(Intent.LIST_PROFILES, limit=2)
        validation = validate_spec(spec)
        self.assertIs(validation.status, ValidationStatus.VALID)
        plan = plan_validation(validation)
        self.assertTrue(plan.supported)
        self.assertEqual(plan.params["limit"], 2)

    def test_forged_valid_result_cannot_bypass_validation(self):
        forged = ValidationResult(
            status=ValidationStatus.VALID,
            spec=QuerySpec(
                Intent.LIST_PROFILES,
                wmo_numbers=("1900001 OR 1=1",),
            ),
        )
        plan = plan_validation(forged)
        self.assertUnsupported(plan)
        self.assertIsNone(plan.sql)

    def test_forged_non_spec_result_is_refused(self):
        forged = ValidationResult(
            status=ValidationStatus.VALID,
            spec="not a QuerySpec",
        )
        plan = plan_validation(forged)
        self.assertUnsupported(plan)
        self.assertIsNone(plan.sql)

    def test_forged_valid_status_with_unsupported_filter_is_refused(self):
        forged = ValidationResult(
            status=ValidationStatus.VALID,
            spec=QuerySpec(
                Intent.LIST_PROFILES,
                pressure_range=(0.0, 100.0),
            ),
        )
        plan = plan_validation(forged)
        self.assertUnsupported(plan)
        self.assertIsNone(plan.sql)

    def test_pressure_filter_is_not_dropped(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                pressure_range=(0.0, 500.0),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("Pressure filtering", plan.reason)

    def test_depth_filter_is_never_converted(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                depth_range_m=(0.0, 500.0),
                depth_requested=True,
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("Depth is not stored", plan.reason)

    def test_measurement_qc_is_unsupported(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                parameters=(Parameter.TEMP,),
                qc_filter=QcFilter(QcScope.MEASUREMENT),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("Measurement-level QC", plan.reason)

    def test_profile_qc_requires_one_core_parameter(self):
        multi = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                parameters=(Parameter.TEMP, Parameter.PSAL),
                qc_filter=good_quality_filter(QcScope.PROFILE),
            )
        )
        self.assertUnsupported(multi)
        self.assertIn("exactly one core parameter", multi.reason)

    def test_profile_qc_maps_parameter_column(self):
        spec = QuerySpec(
            Intent.LIST_PROFILES,
            parameters=(Parameter.TEMP,),
            qc_filter=good_quality_filter(QcScope.PROFILE),
        )
        plan = self.plan(spec)
        self.assertTrue(plan.supported)
        self.assertIn("p.profile_temp_qc IN (:profile_qc_0, :profile_qc_1)",
                      plan.sql)
        qc_values = {
            value for key, value in plan.params.items()
            if key.startswith("profile_qc_")
        }
        self.assertEqual(qc_values, {"A", "B"})

    def test_aggregate_mean_is_explicitly_unsupported(self):
        plan = self.plan(
            QuerySpec(
                Intent.AGGREGATE,
                aggregations=(Aggregation.MEAN,),
                parameters=(Parameter.TEMP,),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("per-level JSON post-processing", plan.reason)

    def test_compare_is_unsupported(self):
        plan = self.plan(
            QuerySpec(
                Intent.COMPARE,
                wmo_numbers=(1900001, 1900002),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("Comparison", plan.reason)

    def test_semantic_discovery_requires_retrieval(self):
        plan = self.plan(
            QuerySpec(
                Intent.SEMANTIC_DISCOVERY,
                semantic_query="warm salty water near the equator",
            )
        )
        self.assertUnsupported(plan)
        self.assertTrue(plan.uses_semantic_retrieval)
        self.assertIn("ChromaDB", plan.reason)

    def test_list_rejects_aggregation(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                aggregations=(Aggregation.COUNT,),
            )
        )
        self.assertUnsupported(plan)

    def test_enumerate_rejects_unsupported_sort(self):
        plan = self.plan(
            QuerySpec(
                Intent.ENUMERATE_FLOATS,
                sort=SortSpec(SortField.LATITUDE, SortDirection.ASC),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("WMO ordering", plan.reason)

    def test_metadata_rejects_unsupported_sort(self):
        plan = self.plan(
            QuerySpec(
                Intent.METADATA_INFO,
                sort=SortSpec(SortField.PROFILE_ID, SortDirection.DESC),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("WMO ordering", plan.reason)

    def test_enumerate_rejects_profile_time_filter(self):
        plan = self.plan(
            QuerySpec(
                Intent.ENUMERATE_FLOATS,
                time_range=TimeRange(start=datetime(2020, 1, 1)),
            )
        )
        self.assertUnsupported(plan)

    def test_metadata_rejects_parameter_filter(self):
        plan = self.plan(
            QuerySpec(
                Intent.METADATA_INFO,
                parameters=(Parameter.TEMP,),
            )
        )
        self.assertUnsupported(plan)

    def test_invalid_sort_field_is_rejected_by_validation(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                sort=SortSpec(field="profile_time; DROP TABLE argo_profiles"),
            )
        )
        self.assertUnsupported(plan)
        self.assertIn("sort", plan.reason.lower())


class SecurityTests(PlannerTestCase):
    def test_values_are_bound_not_interpolated(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                wmo_numbers=(1900001,),
                cycle_numbers=(3,),
            )
        )
        self.assertTrue(plan.supported)
        self.assertNotIn("1900001", plan.sql)
        self.assertEqual(plan.params["wmo_0"], 1900001)
        self.assertEqual(plan.params["cycle_0"], 3)

    def test_semantic_query_text_never_reaches_sql(self):
        payload = "'; DROP TABLE argo_profiles; --"
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                semantic_query=payload,
                notes=(payload,),
            )
        )
        self.assertTrue(plan.supported)
        self.assertNotIn("DROP", plan.sql)
        self.assertNotIn(payload, plan.sql)
        self.assertNotIn(payload, plan.params.values())

    def test_malicious_wmo_is_rejected(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                wmo_numbers=("1900001 OR 1=1",),
            )
        )
        self.assertUnsupported(plan)
        self.assertIsNone(plan.sql)

    def test_excessive_limit_is_clamped_by_validation(self):
        validation = validate_spec(
            QuerySpec(Intent.LIST_PROFILES, limit=10_000)
        )
        self.assertIs(validation.status, ValidationStatus.VALID)
        plan = plan_validation(validation)
        self.assertEqual(plan.limit, 100)
        self.assertEqual(plan.params["limit"], 100)

    def test_invalid_limit_is_rejected(self):
        for limit in (0, -1, "5", 2.5):
            with self.subTest(limit=limit):
                plan = self.plan(
                    QuerySpec(Intent.LIST_PROFILES, limit=limit)
                )
                self.assertUnsupported(plan)

    def test_unknown_parameter_identifier_is_rejected(self):
        plan = self.plan(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=("TEMPERATURE",),
            )
        )
        self.assertUnsupported(plan)


class ScientificIntegrityTests(PlannerTestCase):
    def test_raw_and_adjusted_selection_stay_distinct(self):
        raw = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
                parameters=(Parameter.TEMP,),
            )
        )
        adjusted = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
                parameters=(Parameter.TEMP,),
                values=MeasurementValues.ADJUSTED,
            )
        )
        self.assertTrue(raw.supported and adjusted.supported)
        self.assertIn("p.temperature, p.temperature_qc", raw.sql)
        self.assertNotIn("temperature_adjusted", raw.sql)
        self.assertIn("p.temperature_adjusted", adjusted.sql)
        self.assertIn("p.temperature_adjusted_qc", adjusted.sql)
        self.assertNotIn("p.temperature,", adjusted.sql)

    def test_profile_data_mode_and_parameter_data_mode_are_separate(self):
        plan = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
            )
        )
        self.assertIn("p.data_mode", plan.sql)
        self.assertIn("p.parameter_data_mode", plan.sql)

    def test_profile_and_measurement_qc_are_distinct(self):
        profile_plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                parameters=(Parameter.PSAL,),
                qc_filter=good_quality_filter(QcScope.PROFILE),
            )
        )
        self.assertIn("p.profile_psal_qc", profile_plan.sql)
        measurement_plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                parameters=(Parameter.PSAL,),
                qc_filter=QcFilter(QcScope.MEASUREMENT),
            )
        )
        self.assertUnsupported(measurement_plan)
        self.assertNotIn("salinity_qc", measurement_plan.sql or "")

    def test_null_is_not_treated_as_zero(self):
        plan = self.plan(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.TEMP,),
            )
        )
        self.assertIn("COALESCE(p.temperature, '[]')", plan.sql)
        self.assertNotIn("temperature = 0", plan.sql)
        self.assertNotIn("IFNULL(p.temperature", plan.sql)

    def test_bgc_never_fabricates_a_column(self):
        plan = self.plan(
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.NITRATE,),
            )
        )
        self.assertTrue(plan.supported)
        self.assertIn("p.bgc_params", plan.sql)
        self.assertIn("$.NITRATE", plan.sql)
        self.assertNotIn("p.nitrate", plan.sql)

    def test_missing_parameters_do_not_create_a_filter(self):
        plan = self.plan(QuerySpec(Intent.LIST_PROFILES))
        self.assertNotIn("COALESCE(p.temperature", plan.sql)
        self.assertNotIn("p.temperature, '[]')) > 0", plan.sql)

    def test_detail_exposes_requested_bgc_without_fabrication(self):
        raw = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
                parameters=(Parameter.DOXY,),
            )
        )
        adjusted = self.plan(
            QuerySpec(
                Intent.PROFILE_DETAIL,
                wmo_numbers=(1900001,),
                parameters=(Parameter.DOXY,),
                values=MeasurementValues.ADJUSTED,
            )
        )
        self.assertTrue(raw.supported and adjusted.supported)
        self.assertIn("p.bgc_params, p.bgc_qc", raw.sql)
        self.assertIn("p.bgc_adjusted, p.bgc_adjusted_qc", adjusted.sql)
        self.assertNotIn("p.doxy", raw.sql.lower())

    def test_adjusted_parameter_presence_uses_adjusted_columns(self):
        plan = self.plan(
            QuerySpec(
                Intent.LIST_PROFILES,
                parameters=(Parameter.TEMP,),
                values=MeasurementValues.ADJUSTED,
            )
        )
        self.assertTrue(plan.supported)
        self.assertIn(
            "JSON_LENGTH(COALESCE(p.temperature_adjusted, '[]')) > 0",
            plan.sql,
        )
        self.assertNotIn("COALESCE(p.temperature, '[]')", plan.sql)

    def test_no_fabricated_depth_column(self):
        for spec in (
            QuerySpec(Intent.LIST_PROFILES),
            QuerySpec(
                Intent.PARAMETER_AVAILABILITY,
                parameters=(Parameter.TEMP,),
            ),
        ):
            plan = self.plan(spec)
            self.assertNotIn("depth", (plan.sql or "").lower())


class ExecutionBoundaryTests(unittest.TestCase):
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def fetchall(self):
            return self._rows

    class _Connection:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return ExecutionBoundaryTests._Result(self.rows)

    class _Engine:
        def __init__(self, rows):
            self.connection = ExecutionBoundaryTests._Connection(rows)

        def connect(self):
            return self.connection

    def test_execute_spec_with_bound_params(self):
        spec = QuerySpec(
            Intent.LIST_PROFILES, wmo_numbers=(1900001,), limit=1
        )
        engine = self._Engine([{"profile_id": 1}])
        result = execute_spec(spec, engine)
        self.assertEqual(result["rows"], [{"profile_id": 1}])
        self.assertEqual(result["result_shape"], "profile_rows")
        statement, params = engine.connection.calls[0]
        self.assertIn(":wmo_0", statement)
        self.assertEqual(params["wmo_0"], 1900001)

    def test_execute_refuses_unsupported_spec(self):
        with self.assertRaises(ValueError):
            execute_spec(
                QuerySpec(
                    Intent.LIST_PROFILES,
                    pressure_range=(0.0, 10.0),
                ),
                self._Engine([]),
            )

    def test_execute_no_query_spec_returns_no_rows(self):
        self.assertEqual(
            execute_spec(QuerySpec(Intent.CONVERSATIONAL), self._Engine([])),
            {"rows": [], "result_shape": "none"},
        )


if __name__ == "__main__":
    unittest.main()
