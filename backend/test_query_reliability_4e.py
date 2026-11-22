"""Phase 4E regression tests for defects found by the final evaluation.

Each test names the defect it locks down:

* an analysis question that parses as a valid parameter list was answered
  deterministically as "Found 5 profiles matching the request";
* a per-year float count was answered with the total float list;
* "sort them by cycle" silently fell back to profile-time ordering;
* a measured-value change condition was silently dropped;
* removing a filter with no earlier context inverted the request;
* a failed frontend request kept the previous turn's context.
"""

import unittest
from unittest import mock

from conversation_context import extract_context, merge_followup
from llmbackend import ArgoRAG
from query_parser import parse_query
from query_spec import SortField, ValidationStatus


# ------------------------------------------------------------------
# Fakes (same shape as the Phase 4D tests, no Flask/MySQL/Chroma)
# ------------------------------------------------------------------


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def fetchall(self):
        return list(self._rows)

    def scalar_one(self):
        return self._scalar if self._scalar is not None else len(self._rows)


class _Connection:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement, params=None):
        self._engine.calls.append((str(statement), dict(params or {})))
        return _Result(self._engine.rows)


class FakeEngine:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar
        self.calls = []

    def connect(self):
        return _Connection(self)


def make_rag(engine=None):
    rag = ArgoRAG.__new__(ArgoRAG)
    rag.db_manager = mock.Mock(mysql_engine=engine or FakeEngine())
    return rag


# ------------------------------------------------------------------
# 1. Analysis guard outranks a valid deterministic list answer
# ------------------------------------------------------------------


class AnalysisOutranksDeterministicTests(unittest.TestCase):
    ANALYSIS = (
        "Detect anomalies in the salinity data.",
        "What is the statistical significance of the temperature difference?",
        "Is there a correlation between temperature and pressure?",
        "What relationship exists between temperature and salinity?",
        "How variable is the temperature across the dataset?",
        "Summarise the dataset statistics.",
        "Which floats are behaving strangely?",
        "Are there any outliers in the dataset?",
    )

    def test_analysis_phrasings_are_refused_not_listed(self):
        rag = make_rag()
        rag._retrieve_semantic = mock.Mock(
            side_effect=AssertionError("no retrieval for an analysis question")
        )

        for question in self.ANALYSIS:
            with self.subTest(question=question):
                # These parse as a valid parameter list, which is exactly why
                # the deterministic answer used to win.
                parsed = parse_query(question)
                answer, data = rag.answer_question(question)

                self.assertIn("not implemented", answer)
                self.assertNotIn("matching the request", answer)
                self.assertEqual(data, [])
                self.assertTrue(parsed is not None)

        rag._retrieve_semantic.assert_not_called()

    def test_supported_parameter_list_is_still_answered(self):
        rag = make_rag()

        answer, _ = rag.answer_question("Show salinity profiles")

        self.assertIn("No profiles matched", answer)

    def test_specific_unsupported_operation_still_wins(self):
        rag = make_rag()

        answer, _ = rag.answer_question("What is the average temperature?")

        self.assertIn("MEAN", answer)


# ------------------------------------------------------------------
# 2. Per-period float counts
# ------------------------------------------------------------------


class FloatDistributionTests(unittest.TestCase):
    def test_per_year_float_count_is_not_the_total_list(self):
        rag = make_rag()
        rag._retrieve_available_wmos = mock.Mock(
            side_effect=AssertionError("enumeration must not run")
        )

        answer, data = rag.answer_question(
            "How many floats were active each year?"
        )

        self.assertIn("not implemented", answer)
        self.assertIn("per year", answer)
        self.assertEqual(data, [])
        rag._retrieve_available_wmos.assert_not_called()

    def test_plain_float_count_is_unchanged(self):
        rag = make_rag()
        rag._retrieve_available_wmos = mock.Mock(return_value=[1900121, 2900226])

        answer, _ = rag.answer_question("How many floats are available?")

        self.assertIn("2", answer)


# ------------------------------------------------------------------
# 3. Sorting with an object between the verb and "by"
# ------------------------------------------------------------------


class SortWithObjectTests(unittest.TestCase):
    def test_sort_them_by_cycle_is_parsed(self):
        parsed = parse_query("Show profiles after 2022 and sort them by cycle.")

        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIs(parsed.spec.sort.field, SortField.CYCLE_NUMBER)

    def test_order_those_profiles_by_time_is_parsed(self):
        parsed = parse_query("Order those profiles by time")

        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIs(parsed.spec.sort.field, SortField.PROFILE_TIME)

    def test_sort_them_by_salinity_is_refused(self):
        parsed = parse_query("Show profiles sorted by salinity")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("sorting", (parsed.reason or "").lower())


# ------------------------------------------------------------------
# 4. Measured-value change condition
# ------------------------------------------------------------------


class ChangeConditionTests(unittest.TestCase):
    def test_sudden_temperature_drop_is_clarified(self):
        for question in (
            "Show profiles where temperature dropped suddenly",
            "Find profiles with a sudden rise in salinity",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question)

                self.assertIs(
                    parsed.status, ValidationStatus.NEEDS_CLARIFICATION
                )
                self.assertIn("availability", (parsed.reason or "").lower())


# ------------------------------------------------------------------
# 5. Filter removal
# ------------------------------------------------------------------


class FilterRemovalTests(unittest.TestCase):
    def test_removal_without_context_is_clarified(self):
        rag = make_rag()

        answer, data = rag.answer_question("Remove the temperature filter.")

        self.assertIn("no earlier filter", answer.lower())
        self.assertEqual(data, [])

    def test_removal_with_context_drops_the_parameter(self):
        context = extract_context("Show profiles from 2023 with temperature")
        self.assertIn("TEMP", context["parameters"])

        result = merge_followup(context, "Remove the temperature filter.")

        self.assertIsNotNone(result)
        self.assertIsNone(result.clarification)
        self.assertIn("2023", result.question)
        self.assertNotIn("temperature", result.question)

        parsed = parse_query(result.question)
        self.assertIs(parsed.status, ValidationStatus.VALID)

    def test_removing_an_absent_parameter_clarifies(self):
        context = extract_context("Show profiles from 2023")
        result = merge_followup(context, "Remove the salinity filter.")

        self.assertIsNotNone(result)
        self.assertIn("nothing to remove", result.clarification)


if __name__ == "__main__":
    unittest.main()
