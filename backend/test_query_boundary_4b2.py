"""Phase 4B.2 regression tests: structured-constraint refusal boundary.

Every test asserts routing and constraint safety, not merely an HTTP status:

* ``UnsupportedConstraintRefusalTests`` - an unplannable structured constraint
  is refused with zero SQL and no ChromaDB/Qwen3 fallback.
* ``SemanticPreservationTests``         - genuine discovery questions still
  reach semantic retrieval.
* ``ParserBoundaryRegressionTests``     - cycle + bbox, incomplete cycle and
  incomplete date range.
* ``NaturalLanguageConsistencyTests``   - equivalent phrasings produce the
  same parsed constraints.

No MySQL, ChromaDB or Ollama is contacted.
"""

import unittest
from unittest import mock

import llmbackend
from llmbackend import ArgoRAG
from query_integration import answer_deterministically, has_structured_refinement
from query_parser import parse_query


class _Result:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def mappings(self):
        return self

    def fetchall(self):
        return list(self._rows)


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
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def connect(self):
        return _Connection(self)


def make_rag(engine):
    rag = ArgoRAG.__new__(ArgoRAG)
    rag.db_manager = mock.Mock(mysql_engine=engine)
    rag._retrieve_semantic = mock.Mock(
        side_effect=AssertionError("semantic retrieval must not run")
    )
    return rag


def profile_row(profile_id=1, wmo=1900121, cycle=44):
    return {
        "profile_id": profile_id,
        "wmo_number": wmo,
        "cycle_number": cycle,
        "profile_time": "2020-01-01 00:00:00",
        "latitude": 1.0,
        "longitude": 65.0,
        "data_mode": "D",
        "level_count": 44,
    }


def parsed(question):
    return parse_query(question)


# ------------------------------------------------------------------
# Unsupported structured constraints
# ------------------------------------------------------------------


class UnsupportedConstraintRefusalTests(unittest.TestCase):
    REFUSED = (
        "Show profiles between 0 and 500 dbar",
        "Show temperature profiles with pressure between 0 and 500 dbar",
        "Show profiles with measurement-level QC B",
        "Show profiles with measurement-level QC 1",
        "Show profiles with good quality",
        "Show profiles with acceptable QC",
    )

    def test_each_unsupported_constraint_is_refused_without_fallback(self):
        for question in self.REFUSED:
            with self.subTest(question=question):
                engine = FakeEngine(rows=[profile_row()])
                rag = make_rag(engine)

                answer, data = rag.answer_question(question)

                self.assertTrue(
                    "cannot apply" in answer or "could not run" in answer,
                    f"{question!r} did not produce an explicit refusal: {answer!r}",
                )
                self.assertIn("No data was retrieved", answer)
                self.assertEqual(data, [])
                self.assertEqual(engine.calls, [])
                rag._retrieve_semantic.assert_not_called()

    def test_pressure_refusal_names_the_constraint(self):
        rag = make_rag(FakeEngine())
        answer, data = rag.answer_question(
            "Show profiles between 0 and 500 dbar"
        )

        self.assertIn("Pressure filtering", answer)
        self.assertEqual(data, [])

    def test_generic_quality_refusal_does_not_claim_a_qc_verdict(self):
        rag = make_rag(FakeEngine())
        answer, _ = rag.answer_question("Show profiles with acceptable QC")

        self.assertNotIn("All profiles have acceptable QC", answer)
        self.assertIn("QC", answer)


# ------------------------------------------------------------------
# Genuine semantic questions
# ------------------------------------------------------------------


class SemanticPreservationTests(unittest.TestCase):
    # Phase 4D: analysis-shaped discovery questions are refused explicitly
    # instead of being answered from a handful of retrieved profiles.  The
    # genuine descriptive questions below still use semantic retrieval.
    SEMANTIC = (
        "Explain the distribution of oxygen measurements",
        "What does the data tell us about ocean conditions?",
    )

    ANALYSIS_REFUSED = (
        "Find unusual salinity profiles",
        "Find profiles with unusually low salinity",
        "Which profiles have suspicious temperature measurements?",
        "Are there any anomalous profiles?",
    )

    def test_discovery_questions_are_parsed_as_semantic(self):
        for question in self.SEMANTIC:
            with self.subTest(question=question):
                result = parsed(question)
                self.assertEqual(result.intent.value, "semantic_discovery")
                self.assertEqual(result.status.value, "valid")

    def test_analysis_shaped_questions_are_refused_not_sampled(self):
        for question in self.ANALYSIS_REFUSED:
            with self.subTest(question=question):
                engine = FakeEngine()
                rag = make_rag(engine)
                rag._retrieve_semantic = mock.Mock(
                    side_effect=AssertionError("no semantic sample")
                )

                answer, data = rag.answer_question(question)

                self.assertIn("cannot", answer)
                self.assertEqual(data, [])
                rag._retrieve_semantic.assert_not_called()
                self.assertEqual(engine.calls, [])

    def test_discovery_questions_reach_semantic_retrieval(self):
        for question in self.SEMANTIC:
            with self.subTest(question=question):
                engine = FakeEngine()
                rag = make_rag(engine)
                context = [profile_row()]
                rag._retrieve_semantic = mock.Mock(return_value=context)

                with mock.patch.object(
                    llmbackend.ollama,
                    "generate",
                    return_value={"response": "Generated."},
                ):
                    answer, data = rag.answer_question(question)

                # The answer may be the Python profile summary (when the
                # retrieved context supports one) or the generated text; what
                # matters is that the semantic branch ran and no SQL did.
                self.assertTrue(answer)
                self.assertEqual(data, context)
                rag._retrieve_semantic.assert_called_once()
                self.assertEqual(
                    rag._retrieve_semantic.call_args[0][0], question
                )
                self.assertEqual(engine.calls, [])

    def test_oxygen_distribution_is_not_a_presence_filter(self):
        result = parsed("Explain the distribution of oxygen measurements")
        spec = result.validation.spec

        # A presence list would have answered the wrong question; the
        # parameters may be recorded, but the intent must be semantic.
        self.assertEqual(result.intent.value, "semantic_discovery")
        self.assertTrue(spec.semantic_query)

    def test_unusually_low_salinity_is_not_a_plain_list(self):
        result = parsed("Find profiles with unusually low salinity")
        self.assertEqual(result.intent.value, "semantic_discovery")

    def test_plain_oxygen_list_is_still_structured(self):
        # The semantic rule must not swallow ordinary presence questions.
        result = parsed("Show oxygen profiles from 2023")
        self.assertEqual(result.intent.value, "list_profiles")
        self.assertEqual(
            [parameter.value for parameter in result.validation.spec.parameters],
            ["DOXY"],
        )


# ------------------------------------------------------------------
# Parser boundary regressions
# ------------------------------------------------------------------


class ParserBoundaryRegressionTests(unittest.TestCase):
    def test_cycle_and_bounding_box_are_combined(self):
        question = (
            "Show profiles from cycle 25 within latitude 10 to 20 "
            "and longitude 70 to 80"
        )
        result = parsed(question)
        spec = result.validation.spec

        self.assertEqual(result.status.value, "valid")
        self.assertEqual(spec.cycle_numbers, (25,))
        self.assertEqual(spec.bounding_box.min_latitude, 10.0)
        self.assertEqual(spec.bounding_box.max_latitude, 20.0)
        self.assertEqual(spec.bounding_box.min_longitude, 70.0)
        self.assertEqual(spec.bounding_box.max_longitude, 80.0)

        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        answer, data = rag.answer_question(question)

        self.assertEqual(len(engine.calls), 1)
        sql, params = engine.calls[0]
        self.assertIn("p.cycle_number IN", sql)
        self.assertIn("p.latitude >=", sql)
        self.assertIn("p.longitude >=", sql)
        self.assertEqual(params["cycle_0"], 25)
        self.assertEqual(params["min_latitude"], 10.0)
        self.assertEqual(params["max_longitude"], 80.0)
        self.assertIn("Found 1 profile", answer)
        self.assertEqual(data[0]["profile_id"], 1)

    def test_cycle_number_phrasing_is_accepted(self):
        result = parsed("List profiles for cycle number 25")
        self.assertEqual(result.status.value, "valid")
        self.assertEqual(
            result.validation.spec.cycle_numbers, (25,)
        )

    def test_incomplete_cycle_is_clarified_without_listing_everything(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)

        answer, data = rag.answer_question(
            "Show profiles from WMO 1900121 and cycle"
        )

        self.assertIn("cycle number is missing", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])

    def test_incomplete_date_range_is_clarified(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)

        answer, data = rag.answer_question("Show profiles between 2023 and")

        self.assertIn("date range is incomplete", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])

    def test_complete_date_range_is_unaffected(self):
        result = parsed("Show profiles between 2023 and 2024")
        time_range = result.validation.spec.time_range

        self.assertEqual(time_range.start.date().isoformat(), "2023-01-01")
        self.assertEqual(time_range.end.date().isoformat(), "2024-12-31")


# ------------------------------------------------------------------
# Natural-language consistency
# ------------------------------------------------------------------


class NaturalLanguageConsistencyTests(unittest.TestCase):
    def test_doxy_and_oxygen_are_the_same_parameter(self):
        oxygen = parsed("List profiles containing oxygen measurements during 2023")
        doxy = parsed("List profiles containing DOXY measurements during 2023")

        self.assertEqual(
            [parameter.value for parameter in oxygen.validation.spec.parameters],
            ["DOXY"],
        )
        self.assertEqual(
            [parameter.value for parameter in doxy.validation.spec.parameters],
            ["DOXY"],
        )

    def test_quality_wording_variants_apply_the_same_qc_filter(self):
        direct = parsed("Show profiles with acceptable temperature QC")
        reversed_wording = parsed(
            "List profiles where temperature quality is good"
        )

        for result in (direct, reversed_wording):
            qc = result.validation.spec.qc_filter
            self.assertIsNotNone(qc)
            self.assertEqual(qc.scope.value, "profile")
            self.assertEqual(sorted(qc.flags), ["A", "B"])

    def test_before_and_earlier_than_are_the_same_bound(self):
        before = parsed("Show profiles collected before 2020")
        earlier = parsed("List profiles collected earlier than 2020")

        self.assertEqual(
            before.validation.spec.time_range.end,
            earlier.validation.spec.time_range.end,
        )
        self.assertEqual(
            earlier.validation.spec.time_range.end.date().isoformat(),
            "2019-12-31",
        )

    def test_cycle_phrasings_are_equivalent(self):
        plain = parsed("Show profiles from cycle 25")
        numbered = parsed("List profiles for cycle number 25")

        self.assertEqual(
            plain.validation.spec.cycle_numbers,
            numbered.validation.spec.cycle_numbers,
        )


# ------------------------------------------------------------------
# Strictness must not leak into WMO or supported queries
# ------------------------------------------------------------------


class SupportedQueryPreservationTests(unittest.TestCase):
    def test_supported_structured_query_still_executes(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)

        answer, data = rag.answer_question("Show delayed-mode profiles")

        self.assertEqual(len(engine.calls), 1)
        self.assertIn("p.data_mode = :data_mode", engine.calls[0][0])
        self.assertIn("Found 1 profile", answer)
        self.assertEqual(len(data), 1)

    def test_bare_wmo_keeps_the_legacy_route(self):
        engine = FakeEngine()
        rag = make_rag(engine)
        rag._count_profiles_by_wmo = mock.Mock(return_value=56)
        rag._retrieve_by_wmo = mock.Mock(return_value=[profile_row()])
        rag._build_profile_list_answer = mock.Mock(
            return_value="WMO 1900121 has 56 profiles."
        )

        answer, data = rag.answer_question("Show profiles from WMO 1900121")

        self.assertEqual(answer, "WMO 1900121 has 56 profiles.")
        self.assertEqual(len(data), 1)
        self.assertEqual(engine.calls, [])

    def test_has_structured_refinement_ignores_plain_semantic_questions(self):
        for question in (
            "Find unusual salinity profiles",
            "Find profiles with unusually low salinity",
            "Which profiles have suspicious temperature measurements?",
            "Show me some profiles",
        ):
            with self.subTest(question=question):
                self.assertFalse(has_structured_refinement(question))

        for question in (
            "Show profiles between 0 and 500 dbar",
            "Show profiles with acceptable QC",
            "Show profiles from WMO 1900121 after 2021",
        ):
            with self.subTest(question=question):
                self.assertTrue(has_structured_refinement(question))


if __name__ == "__main__":
    unittest.main()
