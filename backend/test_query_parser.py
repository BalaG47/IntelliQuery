"""Tests for the Phase 3C-C deterministic parser and shadow harness.

The suite is pure Python: no MySQL, ChromaDB, Ollama or NetCDF is imported or
contacted, and no production module is modified.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, time

import query_parser
import query_shadow
from query_parser import parse_query
from query_parser_test_cases import CASES
from query_spec import (
    Aggregation,
    DataMode,
    Intent,
    MeasurementValues,
    Parameter,
    QcScope,
    SortDirection,
    SortField,
    ValidationStatus,
    good_quality_filter,
)


def _parsed_filters(spec) -> dict:
    """Flatten the spec fields the corpus cares about into one mapping."""

    if spec is None:
        return {}
    return {
        "parameters": spec.parameters,
        "wmos": spec.wmo_numbers,
        "cycles": spec.cycle_numbers,
        "values": spec.values,
        "data_mode": spec.data_mode,
        "aggregations": spec.aggregations,
        "qc_scope": spec.qc_filter.scope if spec.qc_filter else None,
        "qc_flags": spec.qc_filter.flags if spec.qc_filter else None,
        "pressure": spec.pressure_range,
        "depth": spec.depth_requested or spec.depth_range_m is not None,
        "limit": spec.limit,
        "sort_field": spec.sort.field,
        "has_time": spec.time_range is not None,
        "has_bbox": spec.bounding_box is not None,
    }


def _case_filters(case) -> dict:
    return {
        "parameters": case.parameters,
        "wmos": case.wmos,
        "cycles": case.cycles,
        "values": case.values,
        "data_mode": case.data_mode,
        "aggregations": case.aggregations,
        "qc_scope": case.qc_scope,
        "qc_flags": case.qc_flags,
        "pressure": case.pressure,
        "depth": case.depth_present,
        "limit": case.limit,
        "sort_field": case.sort_field,
        "has_time": case.has_time_range,
        "has_bbox": case.has_bbox,
    }


def _check_case(test: unittest.TestCase, case) -> None:
    parsed = parse_query(case.query)
    spec = parsed.spec

    test.assertIs(parsed.intent, case.intent, msg=case.label)
    test.assertIs(parsed.status, case.status, msg=case.label)

    if case.parameters:
        test.assertEqual(spec.parameters, case.parameters, msg=case.label)
    if case.wmos:
        test.assertEqual(spec.wmo_numbers, case.wmos, msg=case.label)
    if case.cycles:
        test.assertEqual(spec.cycle_numbers, case.cycles, msg=case.label)
    if case.values is not None:
        test.assertIs(spec.values, case.values, msg=case.label)
    if case.data_mode is not None:
        test.assertIs(spec.data_mode, case.data_mode, msg=case.label)
    if case.aggregations:
        test.assertEqual(spec.aggregations, case.aggregations, msg=case.label)
    if case.qc_scope is not None:
        test.assertIsNotNone(spec.qc_filter, msg=case.label)
        test.assertIs(spec.qc_filter.scope, case.qc_scope, msg=case.label)
    if case.qc_flags is not None:
        test.assertIsNotNone(spec.qc_filter, msg=case.label)
        test.assertEqual(spec.qc_filter.flags, case.qc_flags, msg=case.label)
    if case.pressure is not None:
        test.assertEqual(spec.pressure_range, case.pressure, msg=case.label)
    if case.depth_present is not None:
        test.assertIs(spec.depth_requested, case.depth_present, msg=case.label)
    if case.limit is not None:
        test.assertEqual(spec.limit, case.limit, msg=case.label)
    if case.sort_field is not None:
        test.assertIs(spec.sort.field, case.sort_field, msg=case.label)
    if case.sort_direction is not None:
        test.assertIs(spec.sort.direction, case.sort_direction, msg=case.label)
    if case.has_time_range is not None:
        test.assertIs(
            spec.time_range is not None, case.has_time_range, msg=case.label
        )
    if case.time_end is not None:
        test.assertIsNotNone(spec.time_range, msg=case.label)
        test.assertIsNotNone(spec.time_range.end, msg=case.label)
        test.assertEqual(
            spec.time_range.end.date().isoformat(), case.time_end, msg=case.label
        )
    if case.has_bbox is not None:
        test.assertIs(
            spec.bounding_box is not None, case.has_bbox, msg=case.label
        )
    if case.reason_contains:
        test.assertIsNotNone(parsed.reason, msg=case.label)
        test.assertIn(case.reason_contains.lower(), parsed.reason.lower(), msg=case.label)


class CorpusIntegrityTests(unittest.TestCase):
    def test_corpus_is_large_enough(self):
        self.assertGreaterEqual(len(CASES), 60)

    def test_corpus_queries_are_unique(self):
        queries = [case.query for case in CASES]
        self.assertEqual(len(queries), len(set(queries)))

    def test_corpus_covers_every_intent_used_by_the_parser(self):
        covered = {case.intent for case in CASES}
        for intent in (
            Intent.ENUMERATE_FLOATS,
            Intent.COUNT_PROFILES,
            Intent.LIST_PROFILES,
            Intent.PROFILE_DETAIL,
            Intent.AGGREGATE,
            Intent.COMPARE,
            Intent.PARAMETER_AVAILABILITY,
            Intent.METADATA_INFO,
            Intent.CONVERSATIONAL,
            Intent.SEMANTIC_DISCOVERY,
            Intent.CLARIFY,
        ):
            self.assertIn(intent, covered, msg=intent.value)

    def test_corpus_covers_every_terminal_status(self):
        covered = {case.status for case in CASES}
        self.assertEqual(
            covered,
            {
                ValidationStatus.VALID,
                ValidationStatus.INVALID,
                ValidationStatus.NEEDS_CLARIFICATION,
                ValidationStatus.UNSUPPORTED,
            },
        )


class CorpusBehaviourTests(unittest.TestCase):
    def test_every_case_matches(self):
        for case in CASES:
            with self.subTest(case=case.label):
                _check_case(self, case)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_result(self):
        for case in CASES[:20]:
            with self.subTest(case=case.label):
                first = parse_query(case.query)
                second = parse_query(case.query)
                self.assertEqual(first, second)

    def test_whitespace_and_case_are_normalised(self):
        left = parse_query("Show   TEMPERATURE for WMO 1900121")
        right = parse_query("show temperature for wmo 1900121")
        self.assertIs(left.intent, right.intent)
        self.assertIs(left.status, right.status)
        self.assertEqual(left.spec.parameters, right.spec.parameters)
        self.assertEqual(left.spec.wmo_numbers, right.spec.wmo_numbers)

    def test_empty_question_is_invalid(self):
        parsed = parse_query("   ")
        self.assertIs(parsed.status, ValidationStatus.INVALID)


class ExtractionRuleTests(unittest.TestCase):
    def test_measurement_qc_uses_numeric_flags_only(self):
        parsed = parse_query("Show good-quality temperature measurements")
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.MEASUREMENT)
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))

    def test_profile_qc_uses_letter_categories_only(self):
        parsed = parse_query("Show good-quality profiles")
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.PROFILE)
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"A", "B"}))

    def test_qc_codes_6_and_7_are_never_good_quality(self):
        # ARGO table 2: 6 and 7 are reserved/not used for measurement QC.
        measurement = good_quality_filter(QcScope.MEASUREMENT)
        profile = good_quality_filter(QcScope.PROFILE)
        self.assertNotIn("6", measurement.flags)
        self.assertNotIn("7", measurement.flags)
        self.assertNotIn("6", profile.flags)
        self.assertNotIn("7", profile.flags)

    def test_qc_scopes_are_never_mixed(self):
        measurement = good_quality_filter(QcScope.MEASUREMENT)
        profile = good_quality_filter(QcScope.PROFILE)
        self.assertFalse(measurement.flags & profile.flags)

    def test_ambiguous_good_quality_requests_clarification(self):
        parsed = parse_query("Show good-quality data")
        self.assertIs(parsed.intent, Intent.CLARIFY)
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

    def test_pressure_is_pres_not_depth(self):
        parsed = parse_query("Show pressure between 0 and 500 dbar")
        self.assertIn(Parameter.PRES, parsed.spec.parameters)
        self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0))
        self.assertFalse(parsed.spec.depth_requested)

    def test_depth_never_becomes_pressure(self):
        parsed = parse_query("Show temperature at 500 metres depth")
        self.assertIsNone(parsed.spec.pressure_range)
        self.assertTrue(parsed.spec.depth_requested)
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

    def test_combined_depth_and_pressure_mentions_both(self):
        parsed = parse_query(
            "Show temperature between 0 and 500 dbar at depth 100 metres"
        )
        reason = parsed.reason.lower()
        self.assertIn("depth", reason)
        self.assertIn("pressure", reason)
        self.assertIn("dbar", reason)

    def test_adjusted_requires_explicit_wording(self):
        self.assertIs(
            parse_query("Show temperature").spec.values, MeasurementValues.RAW
        )
        self.assertIs(
            parse_query("Show adjusted temperature").spec.values,
            MeasurementValues.ADJUSTED,
        )

    def test_bare_temperature_is_not_an_aggregation(self):
        parsed = parse_query("Show temperature")
        self.assertIs(parsed.intent, Intent.LIST_PROFILES)
        self.assertEqual(parsed.spec.aggregations, ())

    def test_availability_is_not_aggregation(self):
        parsed = parse_query("Which floats have chlorophyll measurements?")
        self.assertIs(parsed.intent, Intent.PARAMETER_AVAILABILITY)
        self.assertEqual(parsed.spec.aggregations, ())

    def test_named_region_is_not_invented(self):
        parsed = parse_query("Show Arabian Sea temperature")
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertTrue(
            parsed.spec is None or parsed.spec.bounding_box is None
        )

    def test_radius_request_is_unsupported_not_approximated(self):
        parsed = parse_query("Show profiles within 100 km of 15N, 65E")
        self.assertIs(parsed.status, ValidationStatus.UNSUPPORTED)
        self.assertIsNotNone(parsed.spec.radius_search)
        self.assertIsNone(parsed.spec.bounding_box)

    def test_median_is_unsupported(self):
        parsed = parse_query("Show median temperature")
        self.assertIs(parsed.status, ValidationStatus.UNSUPPORTED)
        self.assertIn("median", parsed.reason.lower())

    def test_conversational_phrases_are_recognised(self):
        for phrase in ("Hello", "thanks!", "How are you?"):
            with self.subTest(phrase=phrase):
                self.assertIs(
                    parse_query(phrase).intent, Intent.CONVERSATIONAL
                )

    def test_unrelated_questions_do_not_become_aggregations(self):
        for phrase in ("Tell me a joke", "What is the capital of France?"):
            with self.subTest(phrase=phrase):
                parsed = parse_query(phrase)
                self.assertIs(parsed.intent, Intent.SEMANTIC_DISCOVERY)
                self.assertEqual(parsed.spec.aggregations, ())

    def test_no_fabricated_filters_for_incomplete_query(self):
        parsed = parse_query("Show temperature")
        self.assertEqual(parsed.spec.wmo_numbers, ())
        self.assertIsNone(parsed.spec.time_range)
        self.assertIsNone(parsed.spec.bounding_box)
        self.assertIsNone(parsed.spec.pressure_range)
        self.assertIsNone(parsed.spec.qc_filter)


class MetricsTests(unittest.TestCase):
    """Measures the corpus, and states the numbers explicitly."""

    @classmethod
    def setUpClass(cls):
        cls.results = []
        for case in CASES:
            parsed = parse_query(case.query)
            cls.results.append((case, parsed))

        cls.total = len(cls.results)
        cls.intent_correct = sum(
            1 for case, parsed in cls.results if parsed.intent is case.intent
        )
        cls.status_correct = sum(
            1 for case, parsed in cls.results if parsed.status is case.status
        )
        cls.param_correct = sum(
            1
            for case, parsed in cls.results
            if not case.parameters
            or (parsed.spec and parsed.spec.parameters == case.parameters)
        )
        cls.wmo_correct = sum(
            1
            for case, parsed in cls.results
            if not case.wmos
            or (parsed.spec and parsed.spec.wmo_numbers == case.wmos)
        )
        cls.filter_correct = 0
        cls.filter_total = 0
        for case, parsed in cls.results:
            expected = {
                key: value
                for key, value in _case_filters(case).items()
                if value not in (None, (), False)
            }
            if not expected:
                continue
            cls.filter_total += 1
            actual = _parsed_filters(parsed.spec)
            if all(actual.get(key) == value for key, value in expected.items()):
                cls.filter_correct += 1

        clarifications = [
            (case, parsed)
            for case, parsed in cls.results
            if case.status is ValidationStatus.NEEDS_CLARIFICATION
        ]
        cls.clarification_correct = sum(
            1 for case, parsed in clarifications if parsed.status is case.status
        )
        cls.clarification_total = len(clarifications)

        unsupported = [
            (case, parsed)
            for case, parsed in cls.results
            if case.status is ValidationStatus.UNSUPPORTED
        ]
        cls.unsupported_correct = sum(
            1 for case, parsed in unsupported if parsed.status is case.status
        )
        cls.unsupported_total = len(unsupported)

        cls.false_positives = sum(
            1
            for case, parsed in cls.results
            if case.category in {"negative example", "semantic"}
            and parsed.intent is not case.intent
        )

        cls.shadow_records = query_shadow.evaluate_queries(
            [case.query for case in CASES]
        )
        cls.shadow_summary = query_shadow.summarise(cls.shadow_records)

    def test_intent_accuracy(self):
        accuracy = self.intent_correct / self.total
        self.assertGreaterEqual(accuracy, 0.95)
        self.assertEqual(self.intent_correct, self.total)

    def test_status_accuracy(self):
        self.assertEqual(self.status_correct, self.total)

    def test_parameter_extraction(self):
        self.assertEqual(self.param_correct, self.total)

    def test_wmo_extraction(self):
        self.assertEqual(self.wmo_correct, self.total)

    def test_filter_extraction(self):
        self.assertEqual(self.filter_correct, self.filter_total)

    def test_clarification_accuracy(self):
        self.assertEqual(self.clarification_correct, self.clarification_total)

    def test_unsupported_accuracy(self):
        self.assertEqual(self.unsupported_correct, self.unsupported_total)

    def test_no_false_positive_classifications(self):
        self.assertEqual(self.false_positives, 0)

    def test_shadow_harness_returns_one_record_per_query(self):
        self.assertEqual(len(self.shadow_records), self.total)
        self.assertEqual(self.shadow_summary["total"], self.total)
        self.assertEqual(self.shadow_summary["parser_errors"], 0)


class ShadowModeTests(unittest.TestCase):
    def test_shadow_record_shape_and_agreement_flag(self):
        record = query_shadow.evaluate_query("Show temperature for WMO 1900121")
        self.assertEqual(record.router_route, "mysql_wmo")
        self.assertEqual(record.router_wmo, 1900121)
        self.assertIs(record.parser_intent, "profile_detail")
        self.assertIs(record.agree, True)
        self.assertIsNone(record.parser_error)

    def test_shadow_records_disagreement_without_raising(self):
        record = query_shadow.evaluate_query("Show Arabian Sea temperature")
        self.assertEqual(record.parser_status, "needs_clarification")
        self.assertIs(record.agree, False)
        self.assertIsNone(record.parser_error)

    def test_shadow_handle_parser_exception(self):
        original = query_shadow.parse_query

        def broken(_query):
            raise RuntimeError("boom")

        query_shadow.parse_query = broken
        try:
            record = query_shadow.evaluate_query("Show temperature")
        finally:
            query_shadow.parse_query = original

        self.assertEqual(record.parser_status, "error")
        self.assertIn("boom", record.parser_error)
        self.assertIs(record.agree, False)
        # The router still produced its own decision.
        self.assertTrue(record.router_route)

    def test_summary_counts(self):
        records = query_shadow.evaluate_queries(
            ["Show temperature for WMO 1900121", "Tell me a joke"]
        )
        summary = query_shadow.summarise(records)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["agreements"] + summary["disagreements"], 2)

    def test_report_is_text_and_json(self):
        report = query_shadow.format_report(
            query_shadow.evaluate_queries(["Show temperature"])
        )
        self.assertIn("router", report)
        self.assertIn('"total": 1', report)


class IndependenceTests(unittest.TestCase):
    """The parser must not reach into storage, LLMs or production modules."""

    def test_parser_and_shadow_do_not_import_storage_or_llm_modules(self):
        for module in (query_parser, query_shadow):
            source = inspect.getsource(module)
            for forbidden in (
                "import mysql",
                "chromadb",
                "ollama",
                "netCDF4",
                "llmbackend",
                "database_manager",
                "ingestion",
                "requests",
            ):
                self.assertNotIn(forbidden, source, msg=module.__name__)

    def test_shadow_does_not_modify_the_router(self):
        before = query_shadow.ArgoQueryRouter.route("Show temperature for WMO 1900121")
        query_shadow.evaluate_query("Compare WMO 1900121 and 2900226")
        after = query_shadow.ArgoQueryRouter.route("Show temperature for WMO 1900121")
        self.assertEqual(before, after)

    def test_parser_has_no_side_effect_on_repeat_calls(self):
        for _ in range(3):
            parsed = parse_query("Show good-quality profiles")
            self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"A", "B"}))


class CorrectiveRegressionTests(unittest.TestCase):
    """Regression tests for review findings H1-H4, M1 and L1."""

    # -- H1: QC flag tokenisation ---------------------------------------
    def test_qc_flags_before_cycle_do_not_become_profile(self):
        parsed = parse_query(
            "Show all profiles with temperature and QC flags 1 and 2 and cycle 5"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.MEASUREMENT)
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))
        self.assertEqual(parsed.spec.cycle_numbers, (5,))

    def test_qc_flags_before_other_a_f_words(self):
        for follower in ("cycle 5", "category labels", "chlorophyll context"):
            with self.subTest(follower=follower):
                parsed = parse_query(
                    f"Show temperature with QC flags 1 and 2 and {follower}"
                )
                self.assertIs(parsed.spec.qc_filter.scope, QcScope.MEASUREMENT)
                self.assertEqual(
                    parsed.spec.qc_filter.flags, frozenset({"1", "2"})
                )

    def test_explicit_profile_flags_are_uppercased(self):
        parsed = parse_query("Show profile QC flags A and B")
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.PROFILE)
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"A", "B"}))

    def test_cross_scope_flags_are_invalid(self):
        parsed = parse_query("Show temperature with QC flags 1 and A")
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIn("mix measurement-level", parsed.reason)

    def test_good_quality_policies_unchanged(self):
        self.assertEqual(
            parse_query(
                "Show good-quality temperature measurements"
            ).spec.qc_filter.flags,
            frozenset({"1", "2"}),
        )
        self.assertEqual(
            parse_query("Show good-quality profiles").spec.qc_filter.flags,
            frozenset({"A", "B"}),
        )

    # -- H2: cycle counts -----------------------------------------------
    def test_every_cycle_count_phrase_clarifies(self):
        for query in (
            "Show the first five cycles",
            "Show the oldest three cycles",
            "Show the earliest 2 cycles",
            "Show the latest five cycles",
            "Show the last three cycles for WMO 1900121",
            "Show the newest 2 cycles",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn(
                    "Cycle counts depend on which cycle numbers exist",
                    parsed.reason,
                )
                self.assertIsNone(parsed.spec)

    def test_cycle_count_never_becomes_a_guessed_range_or_limit(self):
        parsed = parse_query("Show the first five cycles")
        self.assertIsNone(parsed.spec)
        self.assertIn("please", parsed.reason.lower())

    def test_explicit_cycles_plus_count_is_a_conflict(self):
        for query in (
            "Show cycles 10 and 11 for the first two cycles",
            "Show cycles 10 to 20 and the latest five cycles",
            "Show the first three cycles, including cycle 10",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn("both explicit cycle numbers", parsed.reason)
                self.assertIsNone(parsed.spec)

    def test_plain_explicit_cycle_list_still_works(self):
        parsed = parse_query("Show cycles 5 and 8")
        self.assertEqual(parsed.spec.cycle_numbers, (5, 8))
        self.assertIsNone(parsed.spec.limit)

    def test_first_n_profiles_still_uses_a_limit(self):
        parsed = parse_query("Show the first five profiles")
        self.assertEqual(parsed.spec.limit, 5)
        self.assertEqual(parsed.spec.cycle_numbers, ())

    # -- H3: cycle ranges ------------------------------------------------
    def test_incomplete_cycle_expressions_clarify(self):
        for query in (
            "Show cycles 10 to 20 and 25",
            "Show cycles 10 and 11, 12 to 15",
            "Show cycles 10 to 20 and 25 to 30",
            "Show cycles 10 to",
            "Show cycles 10-20-30",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn("could not be read completely", parsed.reason)
                self.assertIsNone(parsed.spec)
                self.assertIsNone(parsed.validation.spec)

    def test_complete_cycle_lists_are_preserved(self):
        for query in (
            "Show cycles 10, 11 and 12",
            "Show cycles 10, 11, and 12",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.spec.cycle_numbers, (10, 11, 12))

    def test_cycle_ranges_keep_both_bounds(self):
        for query in (
            "Show cycles 10 to 20",
            "Show cycles 10 through 20",
            "Show cycle range 10 to 20",
            "Show cycles 10-20",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

    def test_reversed_cycle_range_is_invalid(self):
        parsed = parse_query("Show cycles 20 to 10")
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIn("cannot be earlier", parsed.reason)

    def test_oversized_cycle_range_is_rejected(self):
        parsed = parse_query("Show cycles 1 to 500")
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIn("limited to", parsed.reason)

    # -- H1-a: QC flag separator forms ----------------------------------
    def test_qc_flag_separators_include_oxford_comma(self):
        cases = {
            "Show temperature with QC flags 1, 2, and 3": frozenset({"1", "2", "3"}),
            "Show temperature with QC flags 1, 2 and 3": frozenset({"1", "2", "3"}),
            "Show temperature with QC flags 1 and 2": frozenset({"1", "2"}),
            "Show temperature with QC flags 1 & 2": frozenset({"1", "2"}),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(parsed.spec.qc_filter.scope, QcScope.MEASUREMENT)
                self.assertEqual(parsed.spec.qc_filter.flags, expected)

    def test_profile_oxford_comma_flags(self):
        parsed = parse_query("Show profile QC flags A, B, and C")
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.PROFILE)
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"A", "B", "C"}))

    def test_oxford_comma_mixed_scope_is_invalid(self):
        parsed = parse_query("Show temperature with QC flags 1, 2, and A")
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIn("mix measurement-level", parsed.reason)

    def test_oxford_comma_does_not_consume_cycle(self):
        parsed = parse_query(
            "Show temperature with QC flags 1, 2, and 3 and cycle 5"
        )
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2", "3"}))
        self.assertEqual(parsed.spec.cycle_numbers, (5,))

    # -- H4: WMO cues ----------------------------------------------------
    def test_unrelated_seven_digit_number_is_not_a_wmo(self):
        parsed = parse_query("Show order 1234567 status")
        self.assertEqual(parsed.spec.wmo_numbers, ())
        self.assertIsNotNone(parsed.spec.notes)

    def test_uncued_lookup_number_asks_for_an_identifier(self):
        for query in ("Show profiles in 1900121 seconds", "Profile 9999999 please"):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn("identifier cue", parsed.reason)

    def test_cued_identifiers_are_extracted(self):
        for query in (
            "Show WMO 1900121",
            "Show float 1900121",
            "Show platform ID 1900121",
            "Show WMO 1900121 and WMO 1900121",
        ):
            with self.subTest(query=query):
                self.assertEqual(parse_query(query).spec.wmo_numbers, (1900121,))

    def test_invalid_cued_identifier_is_rejected_by_the_validator(self):
        parsed = parse_query("Show WMO 0000000")
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIn("not a valid seven-digit", parsed.reason)

    def test_generic_id_is_not_a_wmo_cue(self):
        for query in (
            "Show order ID 1234567 status",
            "Show customer ID 1234567",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertEqual(parsed.spec.wmo_numbers, ())

    def test_ambiguous_generic_id_lookup_clarifies(self):
        parsed = parse_query("Show profiles for ID 1900121")
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("identifier cue", parsed.reason)

    def test_platform_and_float_id_forms_still_work(self):
        for query in (
            "Show platform ID 1900121",
            "Show float ID 1900121",
        ):
            with self.subTest(query=query):
                self.assertEqual(parse_query(query).spec.wmo_numbers, (1900121,))

    # -- M1: inclusive date ends -----------------------------------------
    def test_iso_range_end_includes_the_final_day(self):
        parsed = parse_query("Show profiles from 2023-01-01 to 2023-06-30")
        self.assertEqual(parsed.spec.time_range.end.date().isoformat(), "2023-06-30")
        self.assertEqual(parsed.spec.time_range.end.time().isoformat(), "23:59:59.999999")

    def test_month_range_end_includes_the_final_day(self):
        parsed = parse_query("Show profiles between January and June 2023")
        self.assertEqual(parsed.spec.time_range.end.date().isoformat(), "2023-06-30")

    def test_year_branches_use_end_of_day(self):
        cases = {
            "Show profiles between 2015 and 2020": "2020-12-31",
            "Show profiles during 2022": "2022-12-31",
            "Show profiles until 2010": "2010-12-31",
            "Show profiles before 2010": "2009-12-31",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                end = parse_query(query).spec.time_range.end
                self.assertEqual(end.date().isoformat(), expected)
                self.assertEqual(end.time().isoformat(), "23:59:59.999999")

    def test_leap_day_still_validated(self):
        self.assertIs(
            parse_query("Show profiles on 2020-02-29").status,
            ValidationStatus.VALID,
        )
        self.assertIs(
            parse_query("Show profiles on 2023-02-29").status,
            ValidationStatus.INVALID,
        )

    # -- L1: QC documentation -------------------------------------------
    def test_reserved_flags_are_documented_next_to_the_constants(self):
        import query_spec

        source = inspect.getsource(query_spec)
        self.assertIn("flags 6 and 7 are", source)
        self.assertIn("reserved", source)


class CycleCompletenessGrammarTests(unittest.TestCase):
    """Third corrective pass: no cycle expression may yield a partial filter."""

    def _assert_clarifies(self, queries):
        for query in queries:
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn("could not be read completely", parsed.reason)
                self.assertIsNone(parsed.spec)
                self.assertIsNone(parsed.validation.spec)

    def test_h3_1_intervening_words_before_more_cycles(self):
        self._assert_clarifies(
            [
                "Show cycles 10 to 20, then cycle 25",
                "Show cycles 10 to 20, then 25",
                "Show cycles 10 to 20, followed by cycle 25",
                "Show cycles 10 to 20 and later cycle 25",
                "Show cycles 10 to 20; then cycle 25",
            ]
        )

    def test_h3_1_additional_instruction_across_clause_boundary(self):
        self._assert_clarifies(
            [
                "Show cycles 10 to 20 with cycle 25",
                "Show cycles 10 to 20 for cycle 25",
                "Show cycles 10 to 20 in cycle 25",
                "Show cycles 10 to 20 on cycle 25",
                "Show cycles 10 to 20 of cycle 25",
                "Show cycles 10 to 20 at cycle 25",
                "Show cycles 10 to 20 during cycle 25",
                "Show cycles 10 to 20 having cycle 25",
                "Show cycles 10 to 20 where cycle 25",
                "Show cycles 10 to 20 sorted by cycle 25",
                "Show cycles 10 and 11 between cycle 12 and 13",
                "Show cycles 10 and 11 from cycle 12 to 13",
                "Show cycles 10 to 20 for WMO 1900121 and cycle 25",
                "Show cycles 10 and 11 with QC flags 1 and 2 for temperature and cycle 25",
            ]
        )

    def test_h3_1_adversarial_boundary_forms(self):
        self._assert_clarifies(
            [
                "Show cycles 10 to 20 and then for temperature cycle 25",
                "Show cycles 10 and 11 for WMO 1900121 and then cycle #25",
                "Show cycles 10 and 11 from 2020 to 2022 and later cycle 12 to 15",
                "Show cycles 10 and 11 for temperature and cycle 25",
                "Show cycles 10 and 11 with QC flags 1 and 2 for temperature and then cycle 30",
                "Show cycles 10 to 20 and cycle #25",
                "Show cycles 10 and 11 and cycle 12 to 15",
                "Show cycles 10 to 20, highest cycle",
                "Show cycles 10 to 20 cycles",
            ]
        )

    def test_h3_1_orphan_numeric_continuations_clarify(self):
        self._assert_clarifies(
            [
                "Show cycles 10 to 20 for WMO 1900121 and 25",
                "Show cycles 10 and 11 for temperature and 25",
                "Show cycles 10 to 20 from 2020 to 2022 and 25",
                "Show cycles 10 to 20 for WMO 1900121 and 25 and 26",
                "Show cycles 10 to 20 for WMO 1900121, 25",
                "Show cycles 10 to 20 for temperature 25",
                "Show cycles 10 to 20 with QC flags 1 and 2 and 25",
                "Show cycles 10 to 20 for WMO 1900121 and #25",
                "Show cycles 10 to 20, 25, 26",
                "Show cycles 10 to 20 for temperature and 25 for WMO 1900121",
                "Show cycles 10 to 20 sorted by cycle and 25",
            ]
        )

    def test_h3_1_orphan_detection_never_creates_a_limit(self):
        for query in (
            "Show cycles 10 to 20 for WMO 1900121 and 25",
            "Show the first five cycles for WMO 1900121 and 25",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIsNone(parsed.spec)
                self.assertIsNone(parsed.validation.spec)
                self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

    def test_orphan_scan_preserves_numeric_filters(self):
        parsed = parse_query("Show cycles 10 to 20 for float 2900228")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.wmo_numbers, (2900228,))
        self.assertIsNone(parsed.spec.limit)

        parsed = parse_query("Show cycles 10 to 20 in 2020")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query("Show cycles 10 to 20 from 2020 to 2022")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query(
            "Show cycles 10 to 20 with QC flags 1, 2, and 3 for temperature"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2", "3"}))

        parsed = parse_query(
            "Show cycles 10 to 20 with pressure between 0 and 500 dbar"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0))

    def test_orphan_scan_ignores_sort_and_explanatory_text(self):
        parsed = parse_query("Show cycles 10 to 20 with cycle information")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

        parsed = parse_query("Show cycles 10 to 20 where cycle numbers are sparse")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

        parsed = parse_query("Show cycles 10 to 20 sorted by cycle")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

    def _assert_rejected(self, queries):
        for query in queries:
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIsNone(parsed.spec)
                self.assertIsNone(parsed.validation.spec)
                self.assertFalse(parsed.is_valid)

    def test_orphan_numbers_anywhere_in_the_suffix(self):
        self._assert_rejected(
            [
                "Show cycles 10 to 20 for temperature 25 for WMO 1900121",
                "Show cycles 10 to 20 for WMO 1900121 25 for temperature",
                "Show cycles 10 to 20 in 2020 25 for WMO 1900121",
                "Show cycles 10 to 20 for temperature 25 in 2020",
                "Show cycles 10 to 20 with pressure 0 to 500 and 25 for temperature",
            ]
        )

    def test_extra_number_after_a_date_expression_clarifies(self):
        self._assert_rejected(
            [
                "Show cycles 10 to 20 between 2020 and 2022 and 2025",
                "Show cycles 10 to 20 from 2020 to 2022 and 2025",
                "Show cycles 10 to 20 in 2020 and 2025",
                "Show cycles 10 to 20 between 2020 and 2022, 2025",
                "Show cycles 10 to 20 between 2020 and 2022 and between 2021 and 2023",
            ]
        )

    def test_cue_protection_stops_at_the_expression_boundary(self):
        self._assert_rejected(
            [
                "Show cycles 10 to 20 with pressure 0-500 and 25 for WMO 1900121",
                "Show cycles 10 to 20 with pressure 0 to 500 and 25 for temperature",
                "Show cycles 10 to 20 at latitude 15 longitude 75 and 25 for WMO 1900121",
            ]
        )

    def test_valid_numeric_expressions_keep_their_values(self):
        parsed = parse_query("Show cycles 10 to 20 for WMO 1900121")
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))

        parsed = parse_query("Show cycles 10 to 20 for float 2900228")
        self.assertEqual(parsed.spec.wmo_numbers, (2900228,))

        parsed = parse_query("Show cycles 10 to 20 in 2020")
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query("Show cycles 10 to 20 from 2020 to 2022")
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query("Show cycles 10 and 11 between 2020 and 2022")
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query(
            "Show cycles 10 to 20 at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(
            (
                parsed.spec.bounding_box.min_latitude,
                parsed.spec.bounding_box.max_latitude,
                parsed.spec.bounding_box.min_longitude,
                parsed.spec.bounding_box.max_longitude,
            ),
            (10.0, 20.0, 70.0, 80.0),
        )

        parsed = parse_query(
            "Show cycles 10 to 20 with QC flags 1 and 2 for temperature"
        )
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))

        parsed = parse_query(
            "Show cycles 10 to 20 with QC flags 1, 2, and 3 for temperature"
        )
        self.assertEqual(
            parsed.spec.qc_filter.flags, frozenset({"1", "2", "3"})
        )

        parsed = parse_query(
            "Show cycles 10 to 20 with pressure between 0 and 500 dbar"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0))

        parsed = parse_query("Show cycles 10 to 20 for temperature at 500 dbar")
        self.assertIs(parsed.status, ValidationStatus.VALID)

        parsed = parse_query("Show cycles 10 to 20 sorted by cycle")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

    def test_bare_year_is_ambiguous_with_a_cycle_number(self):
        queries = [
            "Show cycles 10 to 20 for WMO 1900121 and 2025",
            "Show cycles 10 to 20 for WMO 1900121 2025",
            "Show cycles 10 to 20 for temperature and 2025",
            "Show cycles 10 to 20 with QC flags 1 and 2 for temperature and 2025",
            "Show cycles 10 to 20 at latitude 15 longitude 75 and 2025",
            "Show cycles 10 to 20 with pressure 0 to 500 dbar and 2025",
        ]
        self._assert_rejected(queries)
        for query in queries:
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIn("calendar year or a cycle number", parsed.reason)
                self.assertIsNone(parsed.validation.spec)

    def test_explicit_date_cues_still_select_the_year(self):
        for query in (
            "Show cycles 10 to 20 in 2025",
            "Show cycles 10 to 20 during 2025",
            "Show cycles 10 to 20 year 2025",
            "Show cycles 10 to 20 for WMO 1900121 in 2025",
            "Show cycles 10 to 20 for WMO 1900121 from 2020 to 2022",
            "Show cycles 10 to 20 for WMO 1900121 between 2020 and 2022",
            "Show cycles 10 to 20 from 2020 to 2022",
            "Show cycles 10 to 20 for WMO 1900121 from 2020",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(parsed.status, ValidationStatus.VALID)
                self.assertIsNotNone(parsed.spec.time_range)
                self.assertEqual(
                    parsed.spec.cycle_numbers, tuple(range(10, 21))
                )
                self.assertIsNone(parsed.spec.limit)

    def test_explicit_date_operators_reach_the_date_extractor(self):
        cases = {
            "Show cycles 10 to 20 since 2020": (
                datetime(2020, 1, 1),
                None,
            ),
            "Show cycles 10 to 20 after 2020": (
                datetime(2020, 1, 1),
                None,
            ),
            "Show cycles 10 to 20 before 2022": (
                None,
                datetime.combine(datetime(2021, 12, 31).date(), time.max),
            ),
            "Show cycles 10 to 20 until 2022": (
                None,
                datetime.combine(datetime(2022, 12, 31).date(), time.max),
            ),
        }
        for query, (expected_start, expected_end) in cases.items():
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(parsed.status, ValidationStatus.VALID)
                self.assertEqual(
                    parsed.spec.cycle_numbers, tuple(range(10, 21))
                )
                self.assertEqual(parsed.spec.time_range.start, expected_start)
                self.assertEqual(parsed.spec.time_range.end, expected_end)
                self.assertEqual(
                    parsed.validation.spec.time_range, parsed.spec.time_range
                )
                self.assertEqual(
                    parsed.validation.spec.cycle_numbers,
                    tuple(range(10, 21)),
                )

    def test_date_operators_keep_wmo_filters(self):
        cases = {
            "Show cycles 10 to 20 for WMO 1900121 since 2020": (
                datetime(2020, 1, 1),
                None,
            ),
            "Show cycles 10 to 20 for WMO 1900121 after 2020": (
                datetime(2020, 1, 1),
                None,
            ),
            "Show cycles 10 to 20 for WMO 1900121 before 2022": (
                None,
                datetime.combine(datetime(2021, 12, 31).date(), time.max),
            ),
            "Show cycles 10 to 20 for WMO 1900121 until 2022": (
                None,
                datetime.combine(datetime(2022, 12, 31).date(), time.max),
            ),
        }
        for query, (expected_start, expected_end) in cases.items():
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIs(parsed.status, ValidationStatus.VALID)
                self.assertEqual(parsed.spec.wmo_numbers, (1900121,))
                self.assertEqual(
                    parsed.spec.cycle_numbers, tuple(range(10, 21))
                )
                self.assertEqual(parsed.spec.time_range.start, expected_start)
                self.assertEqual(parsed.spec.time_range.end, expected_end)
                self.assertEqual(
                    parsed.validation.spec.wmo_numbers, (1900121,)
                )

    def test_explicit_cycle_wording_with_four_digits_clarifies(self):
        self._assert_rejected(
            [
                "Show cycles 10 to 20 for WMO 1900121 and cycle 2025",
                "Show cycles 10 to 20 for WMO 1900121 cycle 2025",
            ]
        )

    def test_h3_1_never_turns_the_second_instruction_into_a_limit(self):
        for query in (
            "Show the first five cycles for WMO 1900121 and cycle 25",
            "Show cycles 10 to 20 for WMO 1900121 and cycle 25",
        ):
            with self.subTest(query=query):
                parsed = parse_query(query)
                self.assertIsNone(parsed.spec)
                self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

    def test_sorting_references_are_not_a_second_instruction(self):
        parsed = parse_query("Show cycles 10 to 20 sorted by cycle")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertIsNone(parsed.spec.limit)

        parsed = parse_query("Show cycles 10 to 20 sorted by cycle desc")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

        parsed = parse_query("Show cycles 10 to 20 order by cycle desc")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))

        parsed = parse_query("Show cycles 10 to 20 sorted by highest cycle")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertIs(parsed.spec.sort.field, SortField.CYCLE_NUMBER)
        self.assertIs(parsed.spec.sort.direction, SortDirection.DESC)
        self.assertIsNone(parsed.spec.limit)

    def test_range_with_a_sort_phrase_still_fails_closed(self):
        # Pre-existing conservative behaviour: the comma after the range is
        # residue, so the expression is clarified rather than guessed at.
        self._assert_clarifies(["Show cycles 10 to 20, highest cycle first"])

    def test_legitimate_filters_survive_beside_a_cycle_range(self):
        parsed = parse_query("Show cycles 10 to 20 for WMO 1900121")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))

        parsed = parse_query("Show cycles 10 to 20 for temperature")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.parameters, (Parameter.TEMP,))

        parsed = parse_query("Show cycles 10 and 11 between 2020 and 2022")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query(
            "Show cycles 10 to 20 with QC flags 1 and 2 for temperature"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))

    def test_h3_2_hash_prefixed_values_are_complete(self):
        cases = {
            "Show cycles #10 to #20": tuple(range(10, 21)),
            "Show cycles 10 to #20": tuple(range(10, 21)),
            "Show cycles #10 to 20": tuple(range(10, 21)),
            "Show cycles #10 and #11": (10, 11),
            "Show cycles #10, #11 and #12": (10, 11, 12),
            "Show cycles 10 and #11": (10, 11),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(
                    parse_query(query).spec.cycle_numbers, expected
                )

    def test_h3_3_semicolon_lists(self):
        cases = {
            "Show cycles 10; 11 and 12": (10, 11, 12),
            "Show cycles 10; 11; 12": (10, 11, 12),
            "Show cycles 10; 11, and 12": (10, 11, 12),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(
                    parse_query(query).spec.cycle_numbers, expected
                )

    def test_h3_3_mixed_semicolon_forms_clarify(self):
        self._assert_clarifies(
            [
                "Show cycles 10 to 20; 25",
                "Show cycles 10; 11 to 15",
            ]
        )

    def test_h3_4_parenthesised_expressions(self):
        cases = {
            "Show cycles (10 and 11)": (10, 11),
            "Show cycles (10, 11, 12)": (10, 11, 12),
            "Show cycles (10 to 20)": tuple(range(10, 21)),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(
                    parse_query(query).spec.cycle_numbers, expected
                )

        parsed = parse_query("Show cycles (10 and 11) for WMO 1900121")
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))

    def test_h3_4_malformed_parentheses_clarify(self):
        self._assert_clarifies(
            [
                "Show cycles ((10 and 11))",
                "Show cycles (10 and 11",
                "Show cycles 10 and 11)",
            ]
        )

    def test_unrelated_filters_are_not_cycle_values(self):
        parsed = parse_query("Show cycles 10 to 20 for WMO 1900121")
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))

        parsed = parse_query(
            "Show cycles 10 and 11 for WMO 1900121 from 2020"
        )
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query("Show cycles 10 to 20 for temperature")
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.parameters, (Parameter.TEMP,))

        parsed = parse_query("Show cycles 10 and 11 between 2020 and 2022")
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertIsNotNone(parsed.spec.time_range)

        parsed = parse_query(
            "Show cycles 10 to 20 at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.bounding_box.min_latitude, 10.0)
        self.assertEqual(parsed.spec.bounding_box.max_longitude, 80.0)

        parsed = parse_query(
            "Show cycles 10 and 11 with QC flags 1, 2, and 3 for temperature"
        )
        self.assertEqual(parsed.spec.cycle_numbers, (10, 11))
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2", "3"}))
        self.assertIs(parsed.spec.qc_filter.scope, QcScope.MEASUREMENT)

    def test_sort_wording_is_not_a_cycle_filter(self):
        parsed = parse_query("Show profiles sorted by highest cycle")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.cycle_numbers, ())


class AggregateCombinationTests(unittest.TestCase):
    def test_multiple_aggregations_preserve_enum_order(self):
        parsed = parse_query("Show minimum and maximum temperature")
        self.assertEqual(
            parsed.spec.aggregations, (Aggregation.MIN, Aggregation.MAX)
        )

    def test_count_and_mean(self):
        parsed = parse_query("Count and mean of temperature measurements")
        self.assertEqual(
            parsed.spec.aggregations, (Aggregation.COUNT, Aggregation.MEAN)
        )


class SortingTests(unittest.TestCase):
    def test_default_sort_is_newest_first(self):
        parsed = parse_query("Show temperature")
        self.assertEqual(parsed.spec.sort.direction, SortDirection.DESC)

    def test_highest_cycle_sort(self):
        parsed = parse_query("Show profiles sorted by highest cycle")
        self.assertEqual(parsed.spec.sort.direction, SortDirection.DESC)
        self.assertEqual(parsed.spec.sort.field.value, "cycle_number")


class FilterIntegrityTests(unittest.TestCase):
    """A recognised pressure or coordinate filter must not vanish silently."""

    def _clarifies(self, query: str, reason: str):
        parsed = parse_query(query)
        self.assertIs(
            parsed.status, ValidationStatus.NEEDS_CLARIFICATION, query
        )
        self.assertIsNone(parsed.spec, query)
        self.assertIn(reason, (parsed.reason or "").lower(), query)

    def _invalid(self, query: str, reason: str):
        parsed = parse_query(query)
        self.assertIs(parsed.status, ValidationStatus.INVALID, query)
        self.assertIsNone(parsed.validation.spec, query)
        self.assertIn(reason, (parsed.reason or "").lower(), query)

    def test_unitless_pressure_ranges_clarify(self):
        for query in (
            "Show cycles 10 to 20 with pressure between 0 and 500",
            "Show cycles 10 to 20 with pressure 0 to 500",
            "Show cycles 10 to 20 with pressure 0-500",
        ):
            self._clarifies(query, "needs a unit")

    def test_unit_qualified_pressure_ranges_are_preserved(self):
        for query in (
            "Show cycles 10 to 20 with pressure 0 to 500 dbar",
            "Show cycles 10 to 20 with pressure between 0 and 500 dbar",
            "Show cycles 10 to 20 with pressure from 0 to 500 decibar",
            "Show cycles 10 to 20 with pressure 0 to 500 db",
        ):
            parsed = parse_query(query)
            self.assertIs(parsed.status, ValidationStatus.VALID, query)
            self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0), query)
            self.assertEqual(
                parsed.spec.cycle_numbers, tuple(range(10, 21)), query
            )

    def test_invalid_pressure_values_still_fail(self):
        reversed_range = parse_query(
            "Show cycles 10 to 20 with pressure 500 to 0 dbar"
        )
        self.assertIs(reversed_range.status, ValidationStatus.INVALID)
        self.assertEqual(reversed_range.spec.pressure_range, (500.0, 0.0))
        self._invalid(
            "Show cycles 10 to 20 with pressure 500 to 0 dbar",
            "minimum cannot be greater",
        )
        self._invalid(
            "Show cycles 10 to 20 with pressure -10 to 500 dbar",
            "cannot be negative",
        )
        self._invalid(
            "Show cycles 10 to 20 with pressure 10000 to 20000 dbar",
            "not plausible",
        )

    def test_pressure_is_never_converted_to_or_from_depth(self):
        parsed = parse_query(
            "Show cycles 10 to 20 with pressure 0 to 500 dbar and depth 100 to 200 m"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(parsed.validation.spec)
        self.assertIn(
            "never converted automatically", (parsed.reason or "").lower()
        )

    def test_pressure_combines_with_other_filters(self):
        parsed = parse_query(
            "Show cycles 10 to 20 for WMO 1900121 in 2021 with QC flags 1 and 2 "
            "for temperature and pressure 0 to 500 dbar"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0))
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))
        self.assertEqual(parsed.spec.cycle_numbers, tuple(range(10, 21)))
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))
        self.assertIsNotNone(parsed.spec.time_range)

    def test_single_coordinates_clarify(self):
        for query in (
            "Show cycles 10 to 20 at latitude 15 longitude 75",
            "Show cycles 10 to 20 with latitude 15 longitude 75",
        ):
            self._clarifies(query, "single latitude/longitude point")
        for query in (
            "Show cycles 10 to 20 at latitude 15",
            "Show cycles 10 to 20 at longitude 75",
        ):
            self._clarifies(
                query, "both a latitude and a longitude are required"
            )

    def test_single_out_of_range_coordinates_are_invalid(self):
        self._invalid(
            "Show cycles 10 to 20 at latitude -95 longitude 75",
            "latitude must lie between",
        )
        self._invalid(
            "Show cycles 10 to 20 at latitude 15 longitude 185",
            "longitude must lie between",
        )

    def test_coordinate_ranges_are_preserved(self):
        parsed = parse_query(
            "Show cycles 10 to 20 at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        box = parsed.spec.bounding_box
        self.assertEqual(
            (
                box.min_latitude,
                box.max_latitude,
                box.min_longitude,
                box.max_longitude,
            ),
            (10.0, 20.0, 70.0, 80.0),
        )

    def test_invalid_coordinate_ranges_still_fail(self):
        parsed = parse_query(
            "Show cycles 10 to 20 at latitude 95 and 100 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.INVALID)
        self.assertIsNone(parsed.validation.spec)
        self.assertIn("latitude must lie between", (parsed.reason or "").lower())

    def test_incomplete_coordinate_ranges_clarify(self):
        self._clarifies(
            "Show cycles 10 to 20 at latitude 10 and 20 longitude 70",
            "both a latitude range and a longitude range",
        )

    def test_unsupported_geographic_forms_still_fail_closed(self):
        self._clarifies(
            "Show cycles 10 to 20 in the Arabian Sea", "named ocean region"
        )
        self._clarifies(
            "Show cycles 10 to 20 near 15N 75E within 100 km",
            "could not be read completely",
        )

    def test_coordinates_combine_with_other_filters(self):
        parsed = parse_query(
            "Show cycles 10 to 20 for WMO 1900121 in 2021 with QC flags 1 and 2 "
            "for temperature at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.bounding_box.min_latitude, 10.0)
        self.assertEqual(parsed.spec.bounding_box.max_longitude, 80.0)
        self.assertEqual(parsed.spec.wmo_numbers, (1900121,))
        self.assertEqual(parsed.spec.qc_filter.flags, frozenset({"1", "2"}))


class NinthCorrectiveTests(unittest.TestCase):
    """Single/incomplete pressure and mixed-coordinate fail-closed fixes."""

    def _valid(self, query: str):
        parsed = parse_query(query)
        self.assertIs(parsed.status, ValidationStatus.VALID, query)
        self.assertIsNotNone(parsed.validation.spec, query)
        return parsed.spec

    def _clarifies(self, query: str, reason: str):
        parsed = parse_query(query)
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION, query)
        self.assertIsNone(parsed.validation.spec, query)
        self.assertIn(reason, (parsed.reason or "").lower(), query)

    def _invalid(self, query: str, reason: str):
        parsed = parse_query(query)
        self.assertIs(parsed.status, ValidationStatus.INVALID, query)
        self.assertIsNone(parsed.validation.spec, query)
        self.assertIn(reason, (parsed.reason or "").lower(), query)

    def _bbox(self, spec):
        box = spec.bounding_box
        self.assertIsNotNone(box)
        return (
            box.min_latitude,
            box.max_latitude,
            box.min_longitude,
            box.max_longitude,
        )

    def test_single_pressure_values_are_extracted(self):
        for query in (
            "Show profiles with pressure 500 dbar",
            "Show profiles at pressure 500 dbar",
            "Show profiles with pressure of 500 dbar",
            "Show profiles at 500 dbar",
        ):
            spec = self._valid(query)
            self.assertEqual(spec.pressure_range, (500.0, 500.0), query)
        spec = self._valid("Show profiles where pressure is 500.5 dbar")
        self.assertEqual(spec.pressure_range, (500.5, 500.5))
        spec = self._valid("Show profiles with pressure 500.5 dbar")
        self.assertEqual(spec.pressure_range, (500.5, 500.5))

    def test_unitless_single_pressure_clarifies(self):
        for query in (
            "Show profiles with pressure 500",
            "Show profiles where pressure is 500.5",
        ):
            self._clarifies(query, "pressure value needs a unit")

    def test_repeated_single_pressure_values(self):
        spec = self._valid(
            "Show profiles with pressure 500 dbar and pressure 500 dbar"
        )
        self.assertEqual(spec.pressure_range, (500.0, 500.0))
        for query in (
            "Show profiles with pressure 500 dbar and pressure 700 dbar",
            "Show profiles with pressure 500 dbar and 700 dbar",
        ):
            self._clarifies(query, "more than one pressure value")

    def test_single_pressure_boundaries(self):
        spec = self._valid("Show profiles with pressure 0 dbar")
        self.assertEqual(spec.pressure_range, (0.0, 0.0))
        spec = self._valid("Show profiles with pressure 11000 dbar")
        self.assertEqual(spec.pressure_range, (11000.0, 11000.0))
        self._invalid(
            "Show profiles with pressure -10 dbar", "cannot be negative"
        )
        self._invalid(
            "Show profiles with pressure 12000 dbar", "not plausible"
        )

    def test_incomplete_pressure_ranges_clarify(self):
        for query in (
            "Show profiles with pressure between 0 and",
            "Show profiles with pressure 0 to",
            "Show profiles with pressure between and 500",
            "Show profiles with pressure between",
            "Show profiles with pressure from 0 to",
            "Show profiles with pressure to 500",
        ):
            self._clarifies(query, "pressure range is incomplete")

    def test_pressure_pair_with_extra_value_clarifies(self):
        self._clarifies(
            "Show profiles with pressure 0 to 500 dbar and 700 dbar",
            "more than one pressure",
        )

    def test_valid_pressure_ranges_are_unchanged(self):
        spec = self._valid("Show cycles 10 to 20 with pressure 0 to 500 dbar")
        self.assertEqual(spec.pressure_range, (0.0, 500.0))
        self._clarifies(
            "Show profiles with pressure 0 to 500", "needs a unit"
        )
        self._clarifies(
            "Show profiles with pressure 0 to 500 dbar and depth 100 to 200 m",
            "never converted",
        )

    def test_extra_coordinate_after_bbox_clarifies(self):
        for query in (
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 15",
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and longitude 75",
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 30 and 40 longitude 70 and 80",
        ):
            self._clarifies(query, "only one latitude range and one longitude range")

    def test_invalid_extra_coordinates_are_rejected(self):
        for query, reason in (
            (
                "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 95",
                "latitude must lie between",
            ),
            (
                "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude -95",
                "latitude must lie between",
            ),
            (
                "Show profiles at latitude 10 and 20 longitude 70 and 80 and longitude 185",
                "longitude must lie between",
            ),
            (
                "Show profiles at latitude 10 and 20 longitude 70 and 80 and longitude -185",
                "longitude must lie between",
            ),
            (
                "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 30 and 40 longitude 170 and 185",
                "longitude must lie between",
            ),
        ):
            self._invalid(query, reason)

    def test_repeated_identical_bbox_is_valid(self):
        spec = self._valid(
            "Show profiles at latitude 10 and 20 longitude 70 and 80 "
            "latitude 10 and 20 longitude 70 and 80"
        )
        self.assertEqual(self._bbox(spec), (10.0, 20.0, 70.0, 80.0))

    def test_coordinate_boundaries_are_valid(self):
        spec = self._valid(
            "Show profiles at latitude 80 and 90 longitude 170 and 180"
        )
        self.assertEqual(self._bbox(spec), (80.0, 90.0, 170.0, 180.0))
        spec = self._valid(
            "Show profiles at latitude -90 and -80 longitude -180 and -170"
        )
        self.assertEqual(self._bbox(spec), (-90.0, -80.0, -180.0, -170.0))
        self._invalid(
            "Show profiles at latitude 10 and 90.5 longitude 70 and 80",
            "latitude must lie between",
        )
        self._invalid(
            "Show profiles at latitude 10 and 20 longitude 70 and 180.5",
            "longitude must lie between",
        )

    def test_single_coordinate_behaviour_is_unchanged(self):
        self._clarifies(
            "Show profiles at latitude 15 longitude 75",
            "single latitude/longitude point",
        )
        self._clarifies(
            "Show profiles at latitude 15",
            "both a latitude and a longitude are required",
        )
        spec = self._valid(
            "Show profiles at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertEqual(self._bbox(spec), (10.0, 20.0, 70.0, 80.0))


class TenthCorrectiveTests(unittest.TestCase):
    """Missing-both-bound pressure ranges must never be silently accepted."""

    def _clarifies_incomplete(self, query: str):
        parsed = parse_query(query)
        self.assertIs(
            parsed.status, ValidationStatus.NEEDS_CLARIFICATION, query
        )
        self.assertIsNone(parsed.validation.spec, query)
        self.assertIsNone(parsed.spec, query)
        self.assertIn(
            "pressure range is incomplete", (parsed.reason or "").lower(), query
        )

    def test_missing_both_bounds_clarify(self):
        for query in (
            "Show profiles with pressure between and",
            "Show profiles with pressure from and",
            "Show profiles with pressure between to",
            "Show profiles with pressure to and",
        ):
            self._clarifies_incomplete(query)

    def test_trailing_content_does_not_escape(self):
        for query in (
            "Show profiles with pressure between and.",
            "Show profiles with pressure between and dbar",
            "Show profiles with pressure between and for temperature",
            "Show profiles with pressure to.",
            "Show profiles with pressure and.",
        ):
            self._clarifies_incomplete(query)

    def test_incomplete_with_supported_filters(self):
        for query in (
            "Show cycles 10 to 20 with pressure between and",
            "Show cycles 10 to 20 with pressure from and",
            "Show profiles for WMO 1900121 with pressure between and",
            "Show profiles in 2021 with pressure between and",
            "Show profiles with QC flags 1 and 2 for temperature and pressure between and",
        ):
            self._clarifies_incomplete(query)

    def test_false_positive_protection(self):
        for query in (
            "Show pressure data",
            "Show pressure measurements",
            "Show 25 measurements",
            "Show 500 observations",
            "Show profiles with temperature between 10 and 20",
            "Show cycles 10 to 20",
            "Show profiles with pressure and temperature",
            "Show profiles with pressure and QC flags 1 and 2",
        ):
            parsed = parse_query(query)
            self.assertIs(parsed.status, ValidationStatus.VALID, query)
            self.assertNotIn(
                "pressure range is incomplete",
                (parsed.reason or "").lower(),
                query,
            )

    def test_previously_verified_behaviour_is_preserved(self):
        parsed = parse_query("Show profiles with pressure 500 dbar")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.pressure_range, (500.0, 500.0))

        parsed = parse_query("Show profiles with pressure between 0 and 500 dbar")
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.pressure_range, (0.0, 500.0))

        parsed = parse_query("Show profiles with pressure 500")
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("needs a unit", (parsed.reason or "").lower())

        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIsNotNone(parsed.spec.bounding_box)

        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 15"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 95"
        )
        self.assertIs(parsed.status, ValidationStatus.INVALID)


class EleventhCorrectiveTests(unittest.TestCase):
    """An incomplete pressure range must not hide behind a following clause."""

    def _clarifies_incomplete(self, query: str):
        parsed = parse_query(query)
        self.assertIs(
            parsed.status, ValidationStatus.NEEDS_CLARIFICATION, query
        )
        self.assertIsNone(parsed.validation.spec, query)
        self.assertIsNone(parsed.spec, query)
        self.assertIn(
            "pressure range is incomplete", (parsed.reason or "").lower(), query
        )

    def test_clause_order_after_pressure(self):
        for query in (
            "Show profiles with pressure between cycles 10 to 20",
            "Show profiles with pressure from cycles 10 to 20",
            "Show profiles with pressure to cycles 10 to 20",
            "Show profiles with pressure between WMO 1900121",
            "Show profiles with pressure from WMO 1900121",
            "Show profiles with pressure to WMO 1900121",
            "Show profiles with pressure between in 2021",
            "Show profiles with pressure from in 2021",
            "Show profiles with pressure to in 2021",
            "Show profiles with pressure between with QC flags 1 and 2",
            "Show profiles with pressure from with QC flags 1 and 2",
            "Show profiles with pressure to with QC flags 1 and 2",
            "Show profiles with pressure between for temperature",
            "Show profiles with pressure from for temperature",
            "Show profiles with pressure to for temperature",
        ):
            self._clarifies_incomplete(query)

    def test_clause_order_before_and_surrounding(self):
        for query in (
            "Show cycles 10 to 20 with pressure between and",
            "Show profiles for WMO 1900121 with pressure between and",
            "Show profiles in 2021 with pressure between and",
            "Show profiles with QC flags 1 and 2 for temperature and pressure between and",
            "Show cycles 10 to 20 with pressure between WMO 1900121 in 2021",
            "Show cycles 10 to 20 for WMO 1900121 in 2021 with QC flags 1 and 2 "
            "for temperature and pressure between and",
            "Show profiles with pressure between WMO 1900121 in 2021 with QC "
            "flags 1 and 2 cycles 10 to 20",
        ):
            self._clarifies_incomplete(query)

    def test_unrelated_numbers_are_not_treated_as_bounds(self):
        parsed = parse_query(
            "Show profiles with pressure between WMO 1900121 in 2021 with QC "
            "flags 1 and 2 cycles 10 to 20"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(parsed.validation.spec)
        self.assertIsNone(parsed.spec)

    def test_false_positive_conjunctions(self):
        for query in (
            "Show profiles with pressure data",
            "Show profiles with pressure measurements",
            "Show profiles with pressure and temperature",
            "Show profiles with pressure and QC flags 1 and 2",
            "Show profiles with temperature between 10 and 20",
            "Show cycles 10 to 20",
            "Show profiles with pressure between 0 and 500 dbar",
            "Show cycles 10 to 20 with pressure 500 dbar",
        ):
            parsed = parse_query(query)
            self.assertIs(parsed.status, ValidationStatus.VALID, query)
            self.assertNotIn(
                "pressure range is incomplete",
                (parsed.reason or "").lower(),
                query,
            )
        self.assertEqual(
            parse_query(
                "Show profiles with pressure between 0 and 500 dbar"
            ).spec.pressure_range,
            (0.0, 500.0),
        )
        self.assertEqual(
            parse_query(
                "Show cycles 10 to 20 with pressure 500 dbar"
            ).spec.pressure_range,
            (500.0, 500.0),
        )

    def test_pressure_and_depth_are_preserved(self):
        parsed = parse_query("Show profiles with pressure 500")
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("needs a unit", (parsed.reason or "").lower())

        parsed = parse_query("Show profiles with pressure -10 dbar")
        self.assertIs(parsed.status, ValidationStatus.INVALID)

        parsed = parse_query(
            "Show profiles with pressure 500 dbar and pressure 700 dbar"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

        parsed = parse_query(
            "Show profiles with pressure 0 to 500 dbar and depth 100 to 200 m"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("never converted", (parsed.reason or "").lower())

    def test_coordinate_behaviour_is_preserved(self):
        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80"
        )
        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIsNotNone(parsed.spec.bounding_box)

        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 15"
        )
        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

        parsed = parse_query(
            "Show profiles at latitude 10 and 20 longitude 70 and 80 and latitude 95"
        )
        self.assertIs(parsed.status, ValidationStatus.INVALID)


if __name__ == "__main__":
    unittest.main()
