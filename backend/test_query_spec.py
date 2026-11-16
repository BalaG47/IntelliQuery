"""Unit tests for backend/query_spec.py (Phase 3C-B).

Pure standard-library tests: no MySQL, ChromaDB, Ollama, NetCDF, network or
application initialization.
"""

import dataclasses
import unittest
from datetime import datetime, timezone

from query_spec import (
    APPLICATION_PARAMETERS,
    BGC_PARAMETERS,
    CORE_PARAMETERS,
    DEFAULT_LIST_LIMIT,
    GOOD_MEASUREMENT_QC_FLAGS,
    GOOD_PROFILE_QC_FLAGS,
    GOOD_QC_FLAGS,
    MEASUREMENT_QC_FLAGS,
    PROFILE_QC_NO_QC,
    PROFILE_QC_VALUES,
    MAX_AGGREGATE_PROFILE_SCAN,
    MAX_RESULT_LIMIT,
    MAX_WMO_NUMBERS,
    Aggregation,
    BoundingBox,
    CapabilityProfile,
    DataMode,
    GroupBy,
    Intent,
    IssueCode,
    MeasurementValues,
    Parameter,
    QcFilter,
    QcScope,
    QuerySpec,
    RadiusSearch,
    SortDirection,
    SortField,
    SortSpec,
    TimeRange,
    ValidationStatus,
    good_quality_filter,
    validate_spec,
)


def make_spec(**overrides) -> QuerySpec:
    values = {"intent": Intent.LIST_PROFILES}
    values.update(overrides)
    return QuerySpec(**values)


def validate(**overrides):
    return validate_spec(make_spec(**overrides))


class ModelTests(unittest.TestCase):
    def test_default_values(self):
        spec = QuerySpec(intent=Intent.LIST_PROFILES)

        self.assertEqual(spec.parameters, ())
        self.assertEqual(spec.wmo_numbers, ())
        self.assertEqual(spec.cycle_numbers, ())
        self.assertIsNone(spec.time_range)
        self.assertIsNone(spec.bounding_box)
        self.assertIsNone(spec.pressure_range)
        self.assertIsNone(spec.depth_range_m)
        self.assertFalse(spec.depth_requested)
        self.assertIsNone(spec.radius_search)
        self.assertIsNone(spec.qc_filter)
        self.assertIsNone(spec.data_mode)
        self.assertIs(spec.values, MeasurementValues.RAW)
        self.assertEqual(spec.aggregations, ())
        self.assertIsNone(spec.group_by)
        self.assertEqual(spec.sort.field, SortField.PROFILE_TIME)
        self.assertEqual(spec.sort.direction, SortDirection.DESC)
        self.assertIsNone(spec.limit)
        self.assertIsNone(spec.semantic_query)
        self.assertEqual(spec.notes, ())

    def test_spec_is_immutable(self):
        spec = QuerySpec(intent=Intent.LIST_PROFILES)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.intent = Intent.COMPARE

    def test_replace_produces_a_new_spec(self):
        spec = QuerySpec(intent=Intent.LIST_PROFILES)
        updated = dataclasses.replace(spec, limit=10)

        self.assertIsNone(spec.limit)
        self.assertEqual(updated.limit, 10)

    def test_intent_enum_values(self):
        expected = {
            "ENUMERATE_FLOATS",
            "COUNT_PROFILES",
            "LIST_PROFILES",
            "PROFILE_DETAIL",
            "AGGREGATE",
            "COMPARE",
            "PARAMETER_AVAILABILITY",
            "METADATA_INFO",
            "CONVERSATIONAL",
            "SEMANTIC_DISCOVERY",
            "CLARIFY",
        }
        self.assertEqual({member.name for member in Intent}, expected)
        self.assertIs(Intent("aggregate"), Intent.AGGREGATE)

    def test_parameter_enum_and_groups(self):
        expected = {
            "TEMP",
            "PSAL",
            "PRES",
            "DOXY",
            "CHLA",
            "BBP700",
            "NITRATE",
            "PH_IN_SITU_TOTAL",
        }
        self.assertEqual({member.value for member in Parameter}, expected)
        self.assertIs(Parameter("TEMP"), Parameter.TEMP)
        self.assertEqual(CORE_PARAMETERS, {Parameter.TEMP, Parameter.PSAL, Parameter.PRES})
        self.assertEqual(len(BGC_PARAMETERS), 5)
        self.assertEqual(CORE_PARAMETERS | BGC_PARAMETERS, APPLICATION_PARAMETERS)
        self.assertIs(MeasurementValues("raw"), MeasurementValues.RAW)
        self.assertEqual(DataMode.REAL_TIME.value, "R")
        self.assertEqual(DataMode.DELAYED.value, "D")


class WmoTests(unittest.TestCase):
    def test_valid_single_wmo(self):
        result = validate(wmo_numbers=(1900121,))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.wmo_numbers, (1900121,))

    def test_multiple_valid_wmos(self):
        result = validate(wmo_numbers=(1900121, 2900226))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.wmo_numbers, (1900121, 2900226))

    def test_invalid_length_is_rejected(self):
        for value in (190012, 12345678, 0, -1900121):
            with self.subTest(value=value):
                result = validate(wmo_numbers=(value,))
                self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_non_numeric_wmo_is_rejected(self):
        for value in ("1900121", 1900121.5, True, None):
            with self.subTest(value=value):
                result = validate(wmo_numbers=(value,))
                self.assertEqual(result.status, ValidationStatus.INVALID)
                self.assertIn("WMO", result.reason)

    def test_duplicate_wmos_are_collapsed_with_a_warning(self):
        result = validate(wmo_numbers=(1900121, 1900121))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.wmo_numbers, (1900121,))
        self.assertEqual(result.warnings[0].code, IssueCode.DUPLICATE_WMO_NUMBER)

    def test_too_many_wmos_are_rejected(self):
        numbers = tuple(1900121 + index for index in range(MAX_WMO_NUMBERS + 1))
        result = validate(wmo_numbers=numbers)

        self.assertEqual(result.status, ValidationStatus.INVALID)


class CycleTests(unittest.TestCase):
    def test_valid_cycles(self):
        result = validate(cycle_numbers=(0, 1, 470))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.cycle_numbers, (0, 1, 470))

    def test_negative_cycle_is_rejected(self):
        self.assertEqual(validate(cycle_numbers=(-1,)).status, ValidationStatus.INVALID)

    def test_invalid_cycle_type_is_rejected(self):
        for value in ("4", 4.0, True):
            with self.subTest(value=value):
                self.assertEqual(
                    validate(cycle_numbers=(value,)).status,
                    ValidationStatus.INVALID,
                )

    def test_duplicate_cycles_are_collapsed_with_a_warning(self):
        result = validate(cycle_numbers=(5, 5))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.cycle_numbers, (5,))
        self.assertEqual(
            result.warnings[0].code, IssueCode.DUPLICATE_CYCLE_NUMBER
        )


class TimeRangeTests(unittest.TestCase):
    def test_valid_range(self):
        result = validate(
            time_range=TimeRange(
                start=datetime(2023, 1, 1), end=datetime(2023, 6, 30)
            )
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_reversed_range_is_rejected(self):
        result = validate(
            time_range=TimeRange(
                start=datetime(2023, 6, 30), end=datetime(2023, 1, 1)
            )
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("earlier", result.reason)

    def test_single_boundary_is_allowed(self):
        self.assertEqual(
            validate(time_range=TimeRange(start=datetime(2024, 1, 1))).status,
            ValidationStatus.VALID,
        )
        self.assertEqual(
            validate(time_range=TimeRange(end=datetime(2024, 12, 31))).status,
            ValidationStatus.VALID,
        )

    def test_invalid_date_types_are_rejected(self):
        for value in ("2024-01-01", 20240101, datetime(2024, 1, 1).date()):
            with self.subTest(value=value):
                self.assertEqual(
                    validate(time_range=TimeRange(start=value)).status,
                    ValidationStatus.INVALID,
                )

    def test_mixed_timezone_awareness_is_rejected(self):
        result = validate(
            time_range=TimeRange(
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2024, 6, 1),
            )
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)


class GeographicTests(unittest.TestCase):
    def test_valid_bounding_box(self):
        result = validate(
            bounding_box=BoundingBox(
                min_latitude=0.0,
                max_latitude=30.0,
                min_longitude=45.0,
                max_longitude=78.0,
            )
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_invalid_latitude_is_rejected(self):
        result = validate(
            bounding_box=BoundingBox(-95.0, 90.0, 0.0, 10.0)
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_invalid_longitude_is_rejected(self):
        result = validate(
            bounding_box=BoundingBox(-10.0, 10.0, 0.0, 200.0)
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_reversed_coordinates_are_rejected(self):
        reversed_lat = validate(
            bounding_box=BoundingBox(30.0, 0.0, 45.0, 78.0)
        )
        reversed_lon = validate(
            bounding_box=BoundingBox(0.0, 30.0, 78.0, 45.0)
        )

        self.assertEqual(reversed_lat.status, ValidationStatus.INVALID)
        self.assertEqual(reversed_lon.status, ValidationStatus.INVALID)

    def test_missing_components_are_rejected(self):
        result = validate(
            bounding_box=BoundingBox(min_latitude=0.0, max_latitude=30.0)
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("incomplete", result.reason)

    def test_radius_search_is_unsupported_but_well_formed(self):
        result = validate(radius_search=RadiusSearch(15.0, 65.0, 100.0))

        self.assertEqual(result.status, ValidationStatus.UNSUPPORTED)
        self.assertIn("Radius", result.reason)

    def test_radius_requires_positive_radius(self):
        result = validate(radius_search=RadiusSearch(15.0, 65.0, 0))
        self.assertEqual(result.status, ValidationStatus.INVALID)


class PressureAndDepthTests(unittest.TestCase):
    def test_valid_pressure_range(self):
        result = validate(pressure_range=(0.0, 500.0))

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.pressure_range, (0.0, 500.0))

    def test_negative_pressure_is_rejected(self):
        result = validate(pressure_range=(-1.0, 500.0))
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("negative", result.reason)

    def test_reversed_pressure_range_is_rejected(self):
        self.assertEqual(
            validate(pressure_range=(500.0, 100.0)).status,
            ValidationStatus.INVALID,
        )

    def test_implausible_pressure_is_rejected(self):
        self.assertEqual(
            validate(pressure_range=(0.0, 20000.0)).status,
            ValidationStatus.INVALID,
        )

    def test_depth_request_needs_clarification(self):
        result = validate(depth_range_m=(0.0, 500.0))

        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("dbar", result.reason)
        self.assertIn("not converted", result.reason)
        self.assertIsNone(result.spec)

    def test_no_automatic_depth_to_pressure_conversion(self):
        spec = make_spec(depth_range_m=(0.0, 500.0))
        result = validate_spec(spec)

        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(spec.pressure_range)

    def test_depth_flag_alone_needs_clarification(self):
        self.assertEqual(
            validate(depth_requested=True).status,
            ValidationStatus.NEEDS_CLARIFICATION,
        )

    def test_negative_depth_is_invalid(self):
        self.assertEqual(
            validate(depth_range_m=(-10.0, 500.0)).status,
            ValidationStatus.INVALID,
        )

    def test_pressure_and_depth_remain_distinct(self):
        result = validate(
            pressure_range=(0.0, 500.0), depth_range_m=(0.0, 500.0)
        )
        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)


class QcScopeTests(unittest.TestCase):
    def test_measurement_qc_accepts_numeric_flags(self):
        result = validate(
            parameters=(Parameter.TEMP,),
            qc_filter=QcFilter(
                QcScope.MEASUREMENT, frozenset(MEASUREMENT_QC_FLAGS)
            ),
        )
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(
            result.spec.qc_filter.flags, frozenset(MEASUREMENT_QC_FLAGS)
        )

    def test_measurement_qc_rejects_profile_letters(self):
        for flag in ("A", "B", "F"):
            with self.subTest(flag=flag):
                result = validate(
                    parameters=(Parameter.TEMP,),
                    qc_filter=QcFilter(QcScope.MEASUREMENT, {flag}),
                )
                self.assertEqual(result.status, ValidationStatus.INVALID)
                self.assertIn("0-9", result.reason)

    def test_profile_qc_accepts_codes_and_no_qc_marker(self):
        flags = frozenset({"A", "B", "C", "D", "E", "F", PROFILE_QC_NO_QC})
        self.assertEqual(flags, PROFILE_QC_VALUES)

        result = validate(qc_filter=QcFilter(QcScope.PROFILE, flags))
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertIs(result.spec.qc_filter.scope, QcScope.PROFILE)

    def test_profile_qc_rejects_numeric_flags(self):
        for flag in ("1", "2", "9"):
            with self.subTest(flag=flag):
                result = validate(
                    qc_filter=QcFilter(QcScope.PROFILE, {flag})
                )
                self.assertEqual(result.status, ValidationStatus.INVALID)
                self.assertIn("A-F", result.reason)

    def test_profile_qc_needs_no_parameter(self):
        result = validate(
            qc_filter=QcFilter(QcScope.PROFILE, frozenset({"A", "B"}))
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_measurement_qc_requires_a_parameter(self):
        result = validate(
            qc_filter=QcFilter(QcScope.MEASUREMENT, frozenset({"1", "2"}))
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_empty_filter_is_not_the_no_qc_marker(self):
        for scope in (QcScope.MEASUREMENT, QcScope.PROFILE):
            with self.subTest(scope=scope):
                result = validate(
                    parameters=(Parameter.TEMP,),
                    qc_filter=QcFilter(scope, frozenset()),
                )
                self.assertEqual(result.status, ValidationStatus.INVALID)
                self.assertIn("at least one flag", result.reason)

    def test_invalid_scope_is_rejected(self):
        result = validate(qc_filter=QcFilter("levels", {"1"}))
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_invalid_flag_characters_are_rejected(self):
        for flags in (frozenset({"Z"}), frozenset({"11"}), frozenset({1})):
            with self.subTest(flags=flags):
                result = validate(
                    parameters=(Parameter.TEMP,),
                    qc_filter=QcFilter(QcScope.MEASUREMENT, flags),
                )
                self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_good_quality_defaults_are_scope_specific(self):
        self.assertEqual(GOOD_MEASUREMENT_QC_FLAGS, frozenset({"1", "2"}))
        self.assertEqual(GOOD_PROFILE_QC_FLAGS, frozenset({"A", "B"}))
        self.assertEqual(GOOD_QC_FLAGS, GOOD_MEASUREMENT_QC_FLAGS)

        measurement = good_quality_filter()
        self.assertIs(measurement.scope, QcScope.MEASUREMENT)
        self.assertEqual(measurement.flags, frozenset({"1", "2"}))

        profile = good_quality_filter(QcScope.PROFILE)
        self.assertIs(profile.scope, QcScope.PROFILE)
        self.assertEqual(profile.flags, frozenset({"A", "B"}))

        profile_from_string = good_quality_filter("profile")
        self.assertEqual(profile_from_string.flags, frozenset({"A", "B"}))

    def test_profile_good_quality_is_not_a_measurement_filter(self):
        profile = good_quality_filter(QcScope.PROFILE)
        self.assertFalse(profile.flags & {"1", "2"})
        # A/B means >=75% of levels good, not every level flagged 1 or 2.
        self.assertEqual(profile.flags, frozenset({"A", "B"}))

    def test_flags_remain_configurable(self):
        stricter = good_quality_filter(flags={"1"})
        self.assertEqual(stricter.flags, frozenset({"1"}))
        self.assertNotEqual(stricter, good_quality_filter())


class DataModeAndValuesTests(unittest.TestCase):
    def test_valid_modes(self):
        for mode in (DataMode.REAL_TIME, DataMode.DELAYED, "R", "D"):
            with self.subTest(mode=mode):
                result = validate(data_mode=mode)
                self.assertEqual(result.status, ValidationStatus.VALID)
                self.assertIsInstance(result.spec.data_mode, DataMode)

    def test_invalid_mode_is_rejected(self):
        result = validate(data_mode="X")
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_raw_is_the_default(self):
        self.assertIs(make_spec().values, MeasurementValues.RAW)
        self.assertEqual(validate().status, ValidationStatus.VALID)

    def test_adjusted_requires_explicit_selection(self):
        spec = make_spec(parameters=(Parameter.TEMP,))
        self.assertIs(spec.values, MeasurementValues.RAW)

        result = validate(
            parameters=(Parameter.TEMP,), values=MeasurementValues.ADJUSTED
        )
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertIs(result.spec.values, MeasurementValues.ADJUSTED)

    def test_adjusted_without_parameter_is_rejected(self):
        self.assertEqual(
            validate(values=MeasurementValues.ADJUSTED).status,
            ValidationStatus.INVALID,
        )

    def test_no_automatic_raw_fallback_exists_in_the_model(self):
        spec = make_spec(
            parameters=(Parameter.TEMP,), values=MeasurementValues.ADJUSTED
        )
        result = validate_spec(spec)

        # Validation never rewrites adjusted into raw.
        self.assertIs(result.spec.values, MeasurementValues.ADJUSTED)

    def test_invalid_values_selection_is_rejected(self):
        self.assertEqual(
            validate(parameters=(Parameter.TEMP,), values="corrected").status,
            ValidationStatus.INVALID,
        )


class AggregationTests(unittest.TestCase):
    def test_single_aggregation(self):
        result = validate(
            intent=Intent.AGGREGATE, parameters=(Parameter.TEMP,),
            aggregations=(Aggregation.MEAN,),
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_multiple_aggregations_preserve_order(self):
        result = validate(
            intent=Intent.AGGREGATE, parameters=(Parameter.TEMP,),
            aggregations=(Aggregation.MIN, Aggregation.MAX),
        )
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(
            result.spec.aggregations, (Aggregation.MIN, Aggregation.MAX)
        )

    def test_duplicate_aggregation_is_rejected(self):
        result = validate(
            intent=Intent.AGGREGATE, parameters=(Parameter.TEMP,),
            aggregations=(Aggregation.MIN, Aggregation.MIN),
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("Duplicate", result.reason)

    def test_unsupported_aggregation_is_rejected(self):
        result = validate(
            intent=Intent.AGGREGATE, parameters=(Parameter.TEMP,),
            aggregations=("MEDIAN",),
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_value_aggregation_requires_parameter(self):
        result = validate(
            intent=Intent.AGGREGATE, aggregations=(Aggregation.MEAN,)
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_count_without_parameter_is_valid(self):
        result = validate(
            intent=Intent.AGGREGATE, aggregations=(Aggregation.COUNT,)
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_aggregate_intent_requires_an_operation(self):
        self.assertEqual(
            validate(intent=Intent.AGGREGATE).status,
            ValidationStatus.INVALID,
        )

    def test_grouping_requires_an_aggregation(self):
        self.assertEqual(
            validate(group_by=GroupBy.YEAR).status,
            ValidationStatus.INVALID,
        )

    def test_grouping_with_aggregation_is_valid(self):
        result = validate(
            intent=Intent.AGGREGATE, aggregations=(Aggregation.COUNT,),
            group_by=GroupBy.YEAR,
        )
        self.assertEqual(result.status, ValidationStatus.VALID)


class SortAndLimitTests(unittest.TestCase):
    def test_default_sort(self):
        spec = make_spec()
        self.assertEqual(spec.sort.field, SortField.PROFILE_TIME)
        self.assertEqual(spec.sort.direction, SortDirection.DESC)

    def test_valid_sort(self):
        result = validate(sort=SortSpec(SortField.CYCLE_NUMBER, SortDirection.ASC))
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_invalid_sort_field_and_direction(self):
        self.assertEqual(
            validate(sort=SortSpec("temperature", SortDirection.ASC)).status,
            ValidationStatus.INVALID,
        )
        self.assertEqual(
            validate(sort=SortSpec(SortField.PROFILE_TIME, "sideways")).status,
            ValidationStatus.INVALID,
        )

    def test_documented_limits(self):
        self.assertEqual(DEFAULT_LIST_LIMIT, 5)
        self.assertEqual(MAX_RESULT_LIMIT, 100)
        self.assertEqual(MAX_AGGREGATE_PROFILE_SCAN, 500)
        self.assertNotEqual(MAX_AGGREGATE_PROFILE_SCAN, MAX_RESULT_LIMIT)

    def test_limit_within_range(self):
        result = validate(limit=20)
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.limit, 20)

    def test_invalid_limits(self):
        for value in (0, -10, "10", 5.5, True):
            with self.subTest(value=value):
                self.assertEqual(
                    validate(limit=value).status, ValidationStatus.INVALID
                )

    def test_oversized_limit_is_clamped_with_a_warning(self):
        result = validate(limit=250)

        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertEqual(result.spec.limit, MAX_RESULT_LIMIT)
        self.assertEqual(result.warnings[0].code, IssueCode.LIMIT_CLAMPED)
        self.assertIn("clamped", result.warnings[0].message)


class ValidationStatusTests(unittest.TestCase):
    def test_valid_status(self):
        result = validate(parameters=(Parameter.TEMP,))
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertTrue(result.is_valid)
        self.assertIsNotNone(result.spec)

    def test_invalid_status(self):
        result = validate(wmo_numbers=(123,))
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.spec)
        self.assertTrue(result.reason)

    def test_needs_clarification_status(self):
        result = validate(depth_requested=True)
        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(result.spec)
        self.assertTrue(result.reason)

    def test_unsupported_status(self):
        result = validate(
            parameters=("SALINITY",),
        )
        self.assertEqual(result.status, ValidationStatus.UNSUPPORTED)
        self.assertIn("SALINITY", result.reason)

    def test_invalid_takes_precedence_over_unsupported(self):
        result = validate(wmo_numbers=(1,), radius_search=RadiusSearch(15, 65, 10))

        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_non_spec_input_is_invalid(self):
        self.assertEqual(
            validate_spec("not a spec").status, ValidationStatus.INVALID
        )

    def test_conversational_intent_cannot_carry_filters(self):
        self.assertEqual(
            validate(intent=Intent.CONVERSATIONAL).status,
            ValidationStatus.VALID,
        )
        self.assertEqual(
            validate(
                intent=Intent.CONVERSATIONAL, wmo_numbers=(1900121,)
            ).status,
            ValidationStatus.INVALID,
        )

    def test_semantic_discovery_requires_search_text(self):
        self.assertEqual(
            validate(intent=Intent.SEMANTIC_DISCOVERY).status,
            ValidationStatus.INVALID,
        )
        self.assertEqual(
            validate(
                intent=Intent.SEMANTIC_DISCOVERY,
                semantic_query="cold deep water",
            ).status,
            ValidationStatus.VALID,
        )


class CapabilityTests(unittest.TestCase):
    def test_no_capability_profile_makes_no_availability_claim(self):
        result = validate_spec(
            make_spec(parameters=(Parameter.DOXY,)),
            capabilities=None,
        )

        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_supplied_profile_marks_missing_parameters_unsupported(self):
        capabilities = CapabilityProfile(
            available_parameters=frozenset({Parameter.TEMP, Parameter.PSAL})
        )
        result = validate_spec(
            make_spec(parameters=(Parameter.DOXY,)),
            capabilities=capabilities,
        )

        self.assertEqual(result.status, ValidationStatus.UNSUPPORTED)
        self.assertIn("DOXY", result.reason)

    def test_supplied_profile_accepts_present_parameters(self):
        capabilities = CapabilityProfile(
            available_parameters=frozenset({Parameter.TEMP})
        )
        result = validate_spec(
            make_spec(parameters=(Parameter.TEMP,)), capabilities=capabilities
        )

        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_unknown_availability_reports_none(self):
        self.assertIsNone(CapabilityProfile().supports(Parameter.TEMP))
        known = CapabilityProfile(available_parameters=frozenset({Parameter.TEMP}))
        self.assertTrue(known.supports(Parameter.TEMP))
        self.assertFalse(known.supports(Parameter.DOXY))


class InputTypeGuardTests(unittest.TestCase):
    """H2: malformed input must return INVALID, never raise."""

    def assert_invalid(self, spec, field):
        result = validate_spec(spec)
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn(field, result.reason)

    def test_scalar_wmo_number(self):
        self.assert_invalid(
            QuerySpec(intent=Intent.LIST_PROFILES, wmo_numbers=1900121),
            "wmo_numbers",
        )

    def test_none_wmo_numbers(self):
        self.assert_invalid(
            QuerySpec(intent=Intent.LIST_PROFILES, wmo_numbers=None),
            "wmo_numbers",
        )

    def test_scalar_cycle_number(self):
        self.assert_invalid(
            QuerySpec(intent=Intent.LIST_PROFILES, cycle_numbers=5),
            "cycle_numbers",
        )

    def test_none_parameters(self):
        self.assert_invalid(
            QuerySpec(intent=Intent.LIST_PROFILES, parameters=None),
            "parameters",
        )

    def test_string_parameters_is_not_iterated(self):
        result = validate_spec(
            QuerySpec(intent=Intent.LIST_PROFILES, parameters="TEMP")
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("not a string", result.reason)
        self.assertNotIn("'T'", result.reason)

    def test_string_aggregations_is_not_iterated(self):
        result = validate_spec(
            QuerySpec(
                intent=Intent.AGGREGATE,
                parameters=(Parameter.TEMP,),
                aggregations="MIN",
            )
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("not a string", result.reason)

    def test_invalid_capabilities_object(self):
        result = validate_spec(
            QuerySpec(intent=Intent.LIST_PROFILES),
            capabilities="invalid",
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("capabilities", result.reason)

    def test_validator_never_raises_for_these_inputs(self):
        cases = [
            QuerySpec(intent=Intent.LIST_PROFILES, wmo_numbers=1900121),
            QuerySpec(intent=Intent.LIST_PROFILES, wmo_numbers=None),
            QuerySpec(intent=Intent.LIST_PROFILES, parameters=None),
            QuerySpec(intent=Intent.LIST_PROFILES, parameters="TEMP"),
            QuerySpec(
                intent=Intent.AGGREGATE,
                parameters=(Parameter.TEMP,),
                aggregations="MIN",
            ),
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                try:
                    validate_spec(spec)
                except (TypeError, AttributeError) as error:  # pragma: no cover
                    self.fail(f"validator raised {type(error).__name__}: {error}")

        try:
            validate_spec(
                QuerySpec(intent=Intent.LIST_PROFILES), capabilities="invalid"
            )
        except (TypeError, AttributeError) as error:  # pragma: no cover
            self.fail(f"validator raised {type(error).__name__}: {error}")


class DeepImmutabilityTests(unittest.TestCase):
    """M1: sequence fields must not retain mutable lists."""

    def test_pressure_range_list_is_detached(self):
        source = [0, 500]
        spec = QuerySpec(intent=Intent.LIST_PROFILES, pressure_range=source)

        self.assertEqual(spec.pressure_range, (0, 500))
        self.assertIsInstance(spec.pressure_range, tuple)

        source.append(9999)
        self.assertEqual(spec.pressure_range, (0, 500))

    def test_depth_range_list_is_detached(self):
        source = [0, 500]
        spec = QuerySpec(intent=Intent.LIST_PROFILES, depth_range_m=source)
        source.append(600)

        self.assertEqual(spec.depth_range_m, (0, 500))
        self.assertIsInstance(spec.depth_range_m, tuple)

    def test_notes_list_is_detached(self):
        source = ["first"]
        spec = QuerySpec(intent=Intent.LIST_PROFILES, notes=source)
        source.append("second")

        self.assertEqual(spec.notes, ("first",))
        self.assertIsInstance(spec.notes, tuple)

    def test_validation_normalises_all_sequence_fields(self):
        result = validate_spec(
            QuerySpec(
                intent=Intent.LIST_PROFILES,
                parameters=[Parameter.TEMP],
                wmo_numbers=[1900121],
                cycle_numbers=[1],
                pressure_range=[0, 500],
                depth_range_m=[0, 500],
                notes=["note"],
                aggregations=[Aggregation.COUNT],
            )
        )

        # Depth + pressure yields clarification; validate the fields on a
        # spec without depth to reach the VALID normalisation path.
        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)

        result = validate_spec(
            QuerySpec(
                intent=Intent.LIST_PROFILES,
                parameters=[Parameter.TEMP],
                wmo_numbers=[1900121],
                cycle_numbers=[1],
                pressure_range=[0, 500],
                notes=["note"],
            )
        )
        self.assertEqual(result.status, ValidationStatus.VALID)
        self.assertIsInstance(result.spec.pressure_range, tuple)
        self.assertIsInstance(result.spec.notes, tuple)
        self.assertIsInstance(result.spec.parameters, tuple)
        self.assertIsInstance(result.spec.wmo_numbers, tuple)
        self.assertIsInstance(result.spec.cycle_numbers, tuple)

    def test_spec_with_sequence_fields_is_hashable(self):
        spec = QuerySpec(
            intent=Intent.LIST_PROFILES,
            pressure_range=[0, 500],
            notes=["note"],
        )
        self.assertIsInstance(hash(spec), int)

    def test_frozen_behaviour_is_preserved(self):
        spec = QuerySpec(intent=Intent.LIST_PROFILES, notes=["note"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.notes = ()


class IntentCompletenessTests(unittest.TestCase):
    """M2: minimal per-intent requirements."""

    def test_compare_with_one_wmo_is_invalid(self):
        result = validate(
            intent=Intent.COMPARE, wmo_numbers=(1900121,)
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("comparison requires", result.reason)

    def test_compare_with_no_entities_is_invalid(self):
        self.assertEqual(
            validate(intent=Intent.COMPARE).status, ValidationStatus.INVALID
        )

    def test_compare_with_one_wmo_and_one_parameter_is_invalid(self):
        result = validate(
            intent=Intent.COMPARE,
            wmo_numbers=(1900121,),
            parameters=(Parameter.TEMP,),
        )
        self.assertEqual(result.status, ValidationStatus.INVALID)

    def test_compare_with_two_wmos_is_valid(self):
        result = validate(
            intent=Intent.COMPARE, wmo_numbers=(1900121, 2900226)
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_compare_with_two_parameters_is_valid(self):
        result = validate(
            intent=Intent.COMPARE,
            parameters=(Parameter.TEMP, Parameter.PSAL),
        )
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_parameter_availability_without_parameter_is_invalid(self):
        result = validate(intent=Intent.PARAMETER_AVAILABILITY)
        self.assertEqual(result.status, ValidationStatus.INVALID)
        self.assertIn("parameter", result.reason.lower())

    def test_parameter_availability_with_parameter_is_valid(self):
        result = validate(
            intent=Intent.PARAMETER_AVAILABILITY,
            parameters=(Parameter.DOXY,),
        )
        self.assertEqual(result.status, ValidationStatus.VALID)


class DepthPressureConflictTests(unittest.TestCase):
    """M3: both ranges supplied must be surfaced, never silently dropped."""

    def test_reason_mentions_both_depth_and_pressure(self):
        result = validate(
            pressure_range=(0.0, 500.0), depth_range_m=(0.0, 500.0)
        )

        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        reason = result.reason.lower()
        self.assertIn("depth", reason)
        self.assertIn("pressure", reason)
        self.assertIn("dbar", reason)
        self.assertIn("which range", reason)
        self.assertIsNone(result.spec)

    def test_depth_flag_with_pressure_also_asks(self):
        result = validate(
            depth_requested=True, pressure_range=(0.0, 500.0)
        )
        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("both", result.reason.lower())

    def test_depth_only_keeps_the_original_message(self):
        result = validate(depth_range_m=(0.0, 500.0))
        self.assertEqual(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("not converted", result.reason)


class CapabilityProfileValidationTests(unittest.TestCase):
    """M4: capability contents are normalised and validated."""

    def test_enum_members_are_accepted(self):
        profile = CapabilityProfile({Parameter.TEMP, Parameter.PSAL})
        self.assertEqual(
            profile.available_parameters,
            frozenset({Parameter.TEMP, Parameter.PSAL}),
        )
        self.assertTrue(profile.supports(Parameter.TEMP))

    def test_identifiers_are_normalised(self):
        profile = CapabilityProfile({"temp", "psal"})
        self.assertEqual(
            profile.available_parameters,
            frozenset({Parameter.TEMP, Parameter.PSAL}),
        )
        self.assertTrue(profile.supports(Parameter.PSAL))

    def test_unknown_identifier_raises_value_error(self):
        with self.assertRaises(ValueError):
            CapabilityProfile({"SALINITY"})

    def test_invalid_collection_types_raise(self):
        for value in ("TEMP", 42, {"TEMP": True}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CapabilityProfile(value)

    def test_unknown_availability_is_none(self):
        profile = CapabilityProfile()
        self.assertIsNone(profile.available_parameters)
        self.assertIsNone(profile.supports(Parameter.TEMP))

    def test_known_empty_availability(self):
        profile = CapabilityProfile(frozenset())
        self.assertEqual(profile.available_parameters, frozenset())
        self.assertFalse(profile.supports(Parameter.TEMP))

        result = validate_spec(
            QuerySpec(intent=Intent.LIST_PROFILES, parameters=(Parameter.TEMP,)),
            capabilities=profile,
        )
        self.assertEqual(result.status, ValidationStatus.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
