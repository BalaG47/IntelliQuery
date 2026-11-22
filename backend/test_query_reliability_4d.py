"""Phase 4D regression tests: targeted reliability fixes from Phase 4C.

Covers the four implemented areas without Flask, MySQL, ChromaDB or Ollama:

* unrecognised structured qualifiers are refused, never silently dropped;
* grouped profile counts are executed, unsupported dimensions refused;
* analysis-shaped questions are refused instead of answered from a sample;
* the prompt carries scientific-grounding rules;
* conversational follow-ups replace/add/remove retained filters.
"""

import unittest
from unittest import mock

from conversation_context import (
    extract_context,
    merge_followup,
    sanitise_context,
)
from llmbackend import ArgoRAG
from query_integration import answer_deterministically
from query_parser import parse_query
from query_spec import GroupBy, ValidationStatus


# ------------------------------------------------------------------
# Fakes
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
        if self._engine.scalar is not None and "COUNT(*)" in str(statement):
            return _Result(scalar=self._engine.scalar)
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
# 1. Constraint integrity
# ------------------------------------------------------------------


class ConstraintIntegrity4DTests(unittest.TestCase):
    def test_equator_and_high_salinity_clarifies(self):
        parsed = parse_query("Show profiles near the equator with high salinity")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(parsed.spec)
        self.assertIn("latitude", (parsed.reason or "").lower())

    def test_qualitative_value_conditions_clarify(self):
        for question in (
            "Show profiles with high salinity",
            "Show profiles with low temperature",
            "Show profiles with high oxygen",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question)
                self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
                self.assertIn("availability", (parsed.reason or "").lower())

    def test_pressure_threshold_is_refused_without_sql(self):
        engine = FakeEngine()
        outcome = answer_deterministically(
            "Show profiles with pressure between 0 and 500 dbar",
            engine,
            strict=True,
        )

        self.assertIsNotNone(outcome)
        self.assertIn("cannot apply", outcome.answer)
        self.assertEqual(engine.calls, [])

    def test_depth_threshold_clarifies(self):
        parsed = parse_query("Show profiles at depths below 500 meters")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("pressure", (parsed.reason or "").lower())

    def test_radius_search_clarifies(self):
        parsed = parse_query(
            "Show profiles within 100 km of latitude 10 and longitude 70"
        )

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("coordinate range", (parsed.reason or "").lower())

    def test_unsupported_sort_is_refused(self):
        parsed = parse_query("Show profiles sorted by salinity")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("sorting", (parsed.reason or "").lower())

    def test_supported_cycle_sort_is_planned(self):
        parsed = parse_query("Show profiles from 2023 sorted by cycle number")

        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertEqual(parsed.spec.sort.field.value, "cycle_number")

        engine = FakeEngine(rows=[])
        outcome = answer_deterministically(
            "Show profiles from 2023 sorted by cycle number", engine
        )

        self.assertIsNotNone(outcome)
        self.assertEqual(len(engine.calls), 1)
        self.assertIn("ORDER BY p.cycle_number", engine.calls[0][0])

    def test_unsupported_grouping_dimension_clarifies(self):
        parsed = parse_query("How many profiles are there by data mode?")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIn("data mode", (parsed.reason or "").lower())

    def test_comparison_executes_nothing(self):
        engine = FakeEngine()
        outcome = answer_deterministically(
            "Compare temperature between 2023 and 2024", engine, strict=True
        )

        self.assertIsNotNone(outcome)
        self.assertEqual(engine.calls, [])


# ------------------------------------------------------------------
# 2. Numerical intent
# ------------------------------------------------------------------


class NumericalIntent4DTests(unittest.TestCase):
    def test_count_by_year_uses_grouped_sql(self):
        engine = FakeEngine(
            rows=[
                {"group_key": 2023, "profile_count": 29},
                {"group_key": 2024, "profile_count": 115},
            ]
        )
        outcome = answer_deterministically(
            "How many profiles were collected each year?", engine
        )

        self.assertIsNotNone(outcome)
        self.assertIn("GROUP BY YEAR(p.profile_time)", engine.calls[0][0])
        self.assertIn("Total matching profiles: 144", outcome.answer)
        self.assertIn("2023: 29 profiles", outcome.answer)
        self.assertIn("2024: 115 profiles", outcome.answer)

    def test_count_by_float_is_grouped(self):
        parsed = parse_query("How many profiles per float?")

        self.assertIs(parsed.status, ValidationStatus.VALID)
        self.assertIs(parsed.spec.group_by, GroupBy.WMO)

    def test_plain_total_count_is_unchanged(self):
        engine = FakeEngine(rows=[{"profile_count": 1384}])
        outcome = answer_deterministically("How many profiles are available?", engine)

        self.assertIsNotNone(outcome)
        self.assertNotIn("GROUP BY", engine.calls[0][0])
        self.assertEqual(
            outcome.answer, "There are 1384 profiles matching the request."
        )

    def test_count_by_parameter_is_refused(self):
        parsed = parse_query("How many profiles are there per parameter?")

        self.assertIs(parsed.status, ValidationStatus.NEEDS_CLARIFICATION)

    def test_median_is_refused_without_retrieval(self):
        engine = FakeEngine()
        outcome = answer_deterministically(
            "Calculate the median temperature", engine, strict=True
        )

        self.assertIsNotNone(outcome)
        self.assertIn("median", outcome.answer.lower())
        self.assertEqual(engine.calls, [])


# ------------------------------------------------------------------
# 3. Semantic safeguards
# ------------------------------------------------------------------


class SemanticSafeguardTests(unittest.TestCase):
    ANALYSIS = (
        "Which float has the most anomalous temperature measurements?",
        "Are Indian Ocean salinity values higher than Atlantic?",
        "Find unusually low salinity profiles",
        "Which profiles show suspicious temperature measurements?",
        "Is climate change visible in this dataset?",
        "Do the data show seasonal patterns?",
        "Which profiles show instrument errors?",
    )

    def test_analysis_questions_are_refused_without_retrieval(self):
        rag = make_rag()
        rag._retrieve_semantic = mock.Mock(
            side_effect=AssertionError("no retrieval for analysis")
        )

        for question in self.ANALYSIS:
            with self.subTest(question=question):
                answer, data = rag.answer_question(question)

                self.assertIn("cannot", answer)
                self.assertIn("not implemented", answer)
                self.assertEqual(data, [])

        rag._retrieve_semantic.assert_not_called()

    def test_benign_discovery_is_not_refused(self):
        rag = make_rag()

        self.assertIsNone(
            rag._unsupported_analysis_answer("Show me interesting profiles")
        )
        self.assertIsNone(
            rag._unsupported_analysis_answer("Which floats should I investigate?")
        )

    def test_context_free_count_clarifies(self):
        rag = make_rag()

        answer, data = rag.answer_question("How many are there?")

        self.assertIn("How many of what", answer)
        self.assertEqual(data, [])


# ------------------------------------------------------------------
# 4. Scientific safeguards
# ------------------------------------------------------------------


class ScientificSafeguardTests(unittest.TestCase):
    CONTEXT = [
        {
            "profile_id": 459,
            "wmo_number": 1900121,
            "cycle_number": 61,
            "profile_time": "2020-01-01 00:00:00",
            "latitude": 1.0,
            "longitude": 65.0,
            "pressure": [3116.2, 3116.4],
            "temperature": [30.1, 30.2],
            "salinity": [3.0, 4.9],
            "bgc_params": {},
        }
    ]

    def test_prompt_forbids_depth_substitution(self):
        rag = make_rag()
        prompt = rag._build_prompt("Describe these profiles", self.CONTEXT)

        self.assertIn("never describe a pressure value", prompt.lower())
        self.assertIn("never convert", prompt.lower())
        self.assertIn("near-surface", prompt.lower())

    def test_prompt_marks_the_sample_as_non_representative(self):
        rag = make_rag()
        prompt = rag._build_prompt("Describe these profiles", self.CONTEXT)

        self.assertIn("NOT a statistically representative sample", prompt)
        self.assertIn("1 profile summaries", prompt)

    def test_implausible_values_produce_a_deterministic_caveat(self):
        rag = make_rag()
        context = rag._build_compact_context(self.CONTEXT)

        self.assertIn("DATA-QUALITY NOTE", context)
        self.assertIn("salinity as low as", context)
        self.assertIn("unvalidated and possibly implausible", context)

    def test_prompt_requires_raw_and_qc_caveats(self):
        rag = make_rag()
        prompt = rag._build_prompt("Describe these profiles", self.CONTEXT)

        self.assertIn("raw ARGO measurements", prompt)
        self.assertIn("30-40 PSU", prompt)
        self.assertIn("quality control", prompt)


# ------------------------------------------------------------------
# 5. Conversation context
# ------------------------------------------------------------------


class ConversationContextTests(unittest.TestCase):
    def _turn(self, context, prompt):
        result = merge_followup(context, prompt)
        self.assertIsNotNone(result, prompt)
        self.assertIsNone(result.clarification, prompt)
        parsed = parse_query(result.question)
        self.assertIs(parsed.status, ValidationStatus.VALID, result.question)
        return result

    def test_conversation_a_replaces_and_adds(self):
        context = extract_context("Show profiles from 2023")
        second = self._turn(context, "What about 2024?")

        self.assertIn("2024", second.question)
        self.assertNotIn("2023", second.question)

        third = self._turn(second.context, "Only delayed mode.")

        self.assertIn("2024", third.question)
        self.assertIn("delayed mode", third.question)
        self.assertEqual(third.context["data_mode"], "D")

    def test_conversation_b_preserves_filters(self):
        context = extract_context("Show profiles from WMO 1900121")
        second = self._turn(context, "Now cycle 25.")

        self.assertIn("WMO 1900121", second.question)
        self.assertIn("25", second.question)

        third = self._turn(second.context, "What about temperature?")

        self.assertIn("WMO 1900121", third.question)
        self.assertIn("25", third.question)
        self.assertIn("temperature", third.question)

    def test_conversation_c_counts_the_retained_set(self):
        context = extract_context("Show oxygen profiles")
        second = self._turn(context, "Limit them to 2023.")

        self.assertIn("2023", second.question)
        self.assertIn("oxygen", second.question)

        third = self._turn(second.context, "How many are there?")

        self.assertTrue(third.question.startswith("How many profiles"))
        self.assertIn("2023", third.question)
        self.assertIn("oxygen", third.question)

    def test_explicit_removal(self):
        context = extract_context("Show delayed-mode profiles from 2023")
        result = self._turn(context, "Without delayed mode.")

        self.assertNotIn("delayed mode", result.question)
        self.assertIn("2023", result.question)

    def test_ambiguous_followup_clarifies(self):
        context = extract_context("Show profiles from 2023")
        result = merge_followup(context, "What about those?")

        self.assertIsNotNone(result)
        self.assertIn("could not tell", result.clarification)

    def test_unrelated_question_does_not_reuse_context(self):
        context = extract_context("Show profiles from 2023")

        self.assertIsNone(merge_followup(context, "How many floats are present?"))

    def test_context_is_validated(self):
        self.assertIsNone(sanitise_context("not a dict"))
        cleaned = sanitise_context(
            {
                "wmo_numbers": ["1900121", 1900121, -5],
                "cycle_numbers": [True, 25, 99999],
                "time_start": "2023-02-01",
                "time_end": "2023-01-01",
                "data_mode": "X",
                "parameters": ["TEMP", "NOPE"],
            }
        )

        self.assertEqual(cleaned["wmo_numbers"], [1900121])
        self.assertEqual(cleaned["cycle_numbers"], [25])
        self.assertIsNone(cleaned["time_start"])
        self.assertIsNone(cleaned["data_mode"])
        self.assertEqual(cleaned["parameters"], ["TEMP"])


if __name__ == "__main__":
    unittest.main()
