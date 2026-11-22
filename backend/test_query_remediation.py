"""Phase 4B routing-boundary remediation tests.

Each class covers one audit finding:

* ``MonthNameDateRangeTests``      - F1/F1b: month-name ranges and month-only dates
* ``ConstraintIntegrityTests``     - F3/F4/F5: QC wording, value conditions, subjective words
* ``ParserRefusalTests``           - F6: clarifications and rejections reach the user
* ``WmoRefinementRoutingTests``    - F2: compound WMO questions use the pipeline
* ``LegacyWmoPreservationTests``   - F2/F8: bare lookups keep their route, N is honoured
* ``RemediationEndpointTests``     - the real ``/get-question`` contract

No MySQL, ChromaDB or Ollama is contacted.
"""

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

import flask

import database_manager
import explorer_api
import llmbackend
from llmbackend import ArgoRAG
from query_integration import (
    answer_deterministically,
    has_structured_refinement,
)
from query_parser import parse_query
from query_pipeline import run_pipeline


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
        if self._scalar is not None:
            return self._scalar
        return len(self._rows)


class _Connection:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self._engine.calls.append((sql, dict(params or {})))
        if self._engine.scalar is not None and "COUNT(*)" in sql:
            return _Result(scalar=self._engine.scalar)
        return _Result(self._engine.rows)


class FakeEngine:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar
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


def time_bounds(question):
    parsed = parse_query(question)
    spec = parsed.validation.spec
    if spec is None or spec.time_range is None:
        return None
    start = spec.time_range.start.date().isoformat() if spec.time_range.start else None
    end = spec.time_range.end.date().isoformat() if spec.time_range.end else None
    return start, end


# ------------------------------------------------------------------
# F1 / F1b: month-name dates
# ------------------------------------------------------------------


class MonthNameDateRangeTests(unittest.TestCase):
    def test_two_month_two_year_range_keeps_both_years(self):
        for question in (
            "How many profiles between January 2023 and December 2024?",
            "Show profiles from January 2023 to December 2024.",
            "Show profiles collected between Jan 2023 and Dec 2024.",
            "Jan 2023 through Dec 2024",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    time_bounds(question), ("2023-01-01", "2024-12-31")
                )

    def test_mixed_years_with_full_month_names(self):
        self.assertEqual(
            time_bounds("Show profiles collected from March 2022 to October 2023."),
            ("2022-03-01", "2023-10-31"),
        )

    def test_single_month_is_not_widened_to_the_year(self):
        self.assertEqual(
            time_bounds("Show profiles from December 2023."),
            ("2023-12-01", "2023-12-31"),
        )
        self.assertEqual(
            time_bounds("How many profiles from February 2024?"),
            ("2024-02-01", "2024-02-29"),
        )

    def test_existing_date_forms_are_unchanged(self):
        self.assertEqual(
            time_bounds("Show profiles between January and June 2023"),
            ("2023-01-01", "2023-06-30"),
        )
        self.assertEqual(
            time_bounds("Show profiles during 2022"), ("2022-01-01", "2022-12-31")
        )
        self.assertEqual(
            time_bounds("Show profiles from 2023-01-01 to 2023-06-30"),
            ("2023-01-01", "2023-06-30"),
        )
        self.assertEqual(
            time_bounds("Show profiles between 2023 and 2024"),
            ("2023-01-01", "2024-12-31"),
        )
        # "after YYYY" is inclusive of that year by existing project
        # convention (test_query_parser.test_explicit_date_operators...).
        self.assertEqual(
            time_bounds("Show profiles collected after 2024"), ("2024-01-01", None)
        )

    def test_half_open_end_is_inclusive_of_the_last_day(self):
        parsed = parse_query("Show profiles between January 2023 and December 2024.")
        end = parsed.validation.spec.time_range.end
        self.assertEqual((end.hour, end.minute, end.second), (23, 59, 59))
        self.assertEqual(end.date().isoformat(), "2024-12-31")


class MonthNameDateExecutionTests(unittest.TestCase):
    def test_month_range_count_executes_one_query_with_both_bounds(self):
        engine = FakeEngine(rows=[{"profile_count": 144}])
        answer, data = make_rag(engine).answer_question(
            "How many profiles between January 2023 and December 2024?"
        )

        self.assertEqual(answer, "There are 144 profiles matching the request.")
        self.assertEqual(data, [])
        self.assertEqual(len(engine.calls), 1)
        _, params = engine.calls[0]
        self.assertEqual(str(params["start_time"]), "2023-01-01 00:00:00")
        self.assertEqual(str(params["end_time"]), "2024-12-31 23:59:59.999999")


# ------------------------------------------------------------------
# F3 / F4 / F5: no silent constraint loss
# ------------------------------------------------------------------


class ConstraintIntegrityTests(unittest.TestCase):
    def test_acceptable_qc_phrase_produces_a_qc_filter(self):
        parsed = parse_query("Show profiles with acceptable temperature QC.")
        qc = parsed.validation.spec.qc_filter

        self.assertIsNotNone(qc)
        self.assertEqual(qc.scope.value, "profile")
        self.assertEqual(sorted(qc.flags), ["A", "B"])

    def test_qc_filter_reaches_the_executed_sql(self):
        engine = FakeEngine(rows=[profile_row()])
        answer, _ = make_rag(engine).answer_question(
            "Show profiles with acceptable temperature QC."
        )

        self.assertIn("Found 1 profile", answer)
        sql, params = engine.calls[0]
        self.assertIn("profile_temp_qc", sql)
        self.assertEqual(params["profile_qc_0"], "A")
        self.assertEqual(params["profile_qc_1"], "B")

    def test_good_quality_wording_still_works(self):
        engine = FakeEngine(rows=[profile_row()])
        make_rag(engine).answer_question("Show profiles with good quality temperature")
        self.assertIn("profile_temp_qc", engine.calls[0][0])

    def test_measurement_value_comparison_is_refused_not_converted(self):
        for question in (
            "Show temperature values between 10 and 20 degrees.",
            "Show profiles with salinity above 35 PSU.",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question)
                self.assertEqual(parsed.intent.value, "clarify")
                self.assertEqual(parsed.status.value, "needs_clarification")
                self.assertIn("measured values", parsed.reason)

                engine = FakeEngine(rows=[profile_row()])
                outcome = answer_deterministically(question, engine)

                self.assertIsNotNone(outcome)
                self.assertEqual(outcome.status, "needs_clarification")
                self.assertIn("measured values", outcome.answer)
                self.assertEqual(engine.calls, [])

    def test_pressure_range_is_not_treated_as_a_value_comparison(self):
        parsed = parse_query(
            "Show temperature profiles with pressure between 0 and 500 dbar"
        )
        self.assertEqual(parsed.status.value, "valid")

    def test_subjective_parameter_question_is_not_availability(self):
        for question in (
            "Show profiles with unusual salinity.",
            "Find oxygen-rich profiles.",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question)
                self.assertEqual(parsed.intent.value, "semantic_discovery")
                self.assertIsNone(
                    answer_deterministically(question, FakeEngine())
                )

    def test_genuine_presence_question_is_still_availability(self):
        for question in (
            "Show profiles containing oxygen measurements.",
            "Which profiles contain nitrate?",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question)
                self.assertEqual(parsed.intent.value, "parameter_availability")


# ------------------------------------------------------------------
# F6: parser refusals are surfaced
# ------------------------------------------------------------------


class ParserRefusalTests(unittest.TestCase):
    def test_impossible_cycle_is_clarified_without_any_query(self):
        engine = FakeEngine(rows=[profile_row()])
        answer, data = make_rag(engine).answer_question(
            "Show profiles from cycle 99999."
        )

        self.assertIn("more detail", answer)
        self.assertIn("cycle", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])

    def test_ambiguous_qc_scope_is_clarified(self):
        engine = FakeEngine(rows=[profile_row()])
        answer, data = make_rag(engine).answer_question(
            "Show profiles with good quality measurements."
        )

        self.assertIn("QC scope is ambiguous", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])

    def test_incomplete_coordinates_are_clarified(self):
        outcome = answer_deterministically(
            "Show profiles from 2022 with only longitude 60 and 70.", FakeEngine()
        )
        self.assertEqual(outcome.status, "needs_clarification")
        self.assertIn("latitude", outcome.answer)

    def test_named_region_still_uses_semantic_retrieval(self):
        engine = FakeEngine()
        outcome = answer_deterministically(
            "Find interesting profiles from the Arabian Sea.", engine
        )
        self.assertIsNone(outcome)
        self.assertEqual(engine.calls, [])

    def test_short_wmo_identifier_is_clarified(self):
        engine = FakeEngine(rows=[profile_row()])
        answer, data = make_rag(engine).answer_question("Show profiles for WMO 12.")

        self.assertIn("seven digits", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])

    def test_availability_note_follows_the_list(self):
        engine = FakeEngine(
            rows=[{"profile_id": index, "has_TEMP": 1} for index in range(1, 6)]
        )
        outcome = answer_deterministically(
            "Show profiles containing temperature", engine
        )

        self.assertEqual(outcome.status, "executed")
        self.assertNotIn("profiles Showing", outcome.answer)
        self.assertTrue(
            outcome.answer.rstrip().endswith("were not returned."), outcome.answer
        )

    def test_invalid_spec_is_still_rejected_explicitly(self):
        outcome = answer_deterministically(
            "Show profiles between 2025 and 2010.", FakeEngine()
        )
        self.assertEqual(outcome.status, "invalid")
        self.assertIn("could not run that query", outcome.answer)


# ------------------------------------------------------------------
# F2: compound WMO routing
# ------------------------------------------------------------------


class WmoRefinementRoutingTests(unittest.TestCase):
    def test_refinement_detection_matches_the_intended_boundary(self):
        expectations = {
            "Show WMO 1900121": False,
            "Show profiles from WMO 1900121": False,
            "Show temperature for WMO 1900121": False,
            "Show the latest profile for WMO 1900121": False,
            "Show the 2 latest profiles for WMO 1900121": False,
            "How many profiles for WMO 1900121?": False,
            "List 10 profiles from WMO 1900121": False,
            "Show profiles from WMO 1900121 collected after 2021.": True,
            "Show oxygen profiles from WMO 1900121.": True,
            "Show profiles from WMO 1900121 during cycle 25.": True,
            "Show profiles from WMO 1900121 and WMO 2900226.": True,
            "Show profiles from WMO 1900121 with pressure between 0 and 500 dbar.": True,
        }

        for question, expected in expectations.items():
            with self.subTest(question=question):
                self.assertIs(has_structured_refinement(question), expected)

    def test_wmo_plus_date_applies_both_filters(self):
        engine = FakeEngine(rows=[])
        answer, data = make_rag(engine).answer_question(
            "Show profiles from WMO 1900121 collected after 2021."
        )

        self.assertEqual(answer, "No profiles matched the requested filters.")
        self.assertEqual(data, [])
        sql, params = engine.calls[0]
        self.assertIn("f.wmo_number IN", sql)
        self.assertIn("p.profile_time >=", sql)
        self.assertEqual(params["wmo_0"], 1900121)
        self.assertEqual(str(params["start_time"]), "2021-01-01 00:00:00")

    def test_wmo_plus_parameter_applies_presence_filter(self):
        engine = FakeEngine(rows=[])
        answer, _ = make_rag(engine).answer_question(
            "Show oxygen profiles from WMO 1900121."
        )

        self.assertIn("No profile matched", answer)
        sql, params = engine.calls[0]
        self.assertIn("$.DOXY", sql)
        self.assertEqual(params["wmo_0"], 1900121)

    def test_wmo_plus_cycle_applies_cycle_filter(self):
        engine = FakeEngine(rows=[])
        make_rag(engine).answer_question(
            "Show profiles from WMO 1900121 during cycle 25."
        )

        sql, params = engine.calls[0]
        self.assertIn("p.cycle_number IN", sql)
        self.assertEqual(params["cycle_0"], 25)

    def test_two_wmos_are_both_applied_and_listed(self):
        engine = FakeEngine(rows=[profile_row(wmo=1900121)])
        answer, data = make_rag(engine).answer_question(
            "Show profiles from WMO 1900121 and WMO 2900226."
        )

        self.assertIn("Found 1 profile", answer)
        self.assertEqual(data[0]["wmo_number"], 1900121)
        sql, params = engine.calls[0]
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual((params["wmo_0"], params["wmo_1"]), (1900121, 2900226))

    def test_count_with_parameter_keeps_both_conditions(self):
        engine = FakeEngine(rows=[{"profile_count": 0}])
        answer, _ = make_rag(engine).answer_question(
            "How many oxygen profiles are there for WMO 1900121?"
        )

        self.assertEqual(answer, "There are 0 profiles matching the request.")
        sql, params = engine.calls[0]
        self.assertIn("COUNT(*)", sql)
        self.assertIn("$.DOXY", sql)
        self.assertEqual(params["wmo_0"], 1900121)

    def test_unsupported_compound_wmo_is_refused_not_silently_dropped(self):
        engine = FakeEngine(rows=[profile_row()])
        answer, data = make_rag(engine).answer_question(
            "Show temperature profiles for WMO 1900121 "
            "with pressure between 0 and 500 dbar."
        )

        self.assertIn("cannot apply every part", answer)
        self.assertIn("pressure", answer.lower())
        self.assertEqual(data, [])
        # Neither the deterministic executor nor the legacy route ran a query.
        self.assertEqual(engine.calls, [])

    def test_wmo_refinement_does_not_execute_twice(self):
        engine = FakeEngine(rows=[profile_row()])
        make_rag(engine).answer_question(
            "Show profiles from WMO 1900121 collected after 2021."
        )
        self.assertEqual(len(engine.calls), 1)


# ------------------------------------------------------------------
# F2 / F8: legacy routes preserved
# ------------------------------------------------------------------


class LegacyWmoPreservationTests(unittest.TestCase):
    def _rag(self):
        rag = make_rag(FakeEngine())
        rag._retrieve_by_wmo = mock.Mock(return_value=[profile_row()])
        rag._count_profiles_by_wmo = mock.Mock(return_value=56)
        rag._answer_with_query_spec = mock.Mock(
            side_effect=AssertionError("pipeline must not be consulted")
        )
        return rag

    def test_bare_wmo_keeps_the_legacy_route(self):
        rag = self._rag()
        rag.answer_question("Show WMO 1900121")
        rag._retrieve_by_wmo.assert_called_once()

    def test_core_parameter_detail_keeps_the_legacy_route(self):
        rag = self._rag()
        rag.answer_question("Show temperature for WMO 1900121")
        rag._retrieve_by_wmo.assert_called_once()

    def test_latest_profile_keeps_the_legacy_route(self):
        rag = self._rag()
        answer, data = rag.answer_question(
            "Show the latest profile for WMO 1900121"
        )

        self.assertEqual(len(data), 1)
        self.assertIn("most recent one", answer)

    def test_wmo_count_keeps_its_exact_route(self):
        rag = self._rag()
        answer, data = rag.answer_question("How many profiles for WMO 1900121?")

        self.assertEqual(answer, "WMO 1900121 contains 56 profiles.")
        self.assertEqual(data, [])

    def test_explicit_profile_count_is_honoured(self):
        rag = self._rag()
        rag.answer_question("List 10 profiles from WMO 1900121")

        # The explicit N reaches the legacy retrieval instead of the total.
        self.assertEqual(rag._retrieve_by_wmo.call_args[0][1], 10)


# ------------------------------------------------------------------
# Endpoint contract
# ------------------------------------------------------------------


class _FakeDBManager:
    def __init__(self, *args, **kwargs):
        pass

    def create_mysql_tables(self):
        pass


class _FakeRAG:
    def __init__(self, db_manager=None):
        self.db_manager = db_manager

    def answer_question(self, question):
        raise AssertionError("answer_question was not configured for this test")


def _fake_blueprint(db_manager=None):
    return flask.Blueprint("fake_explorer_remediation", __name__)


def load_ingestion_module():
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as temp_dir:
        os.chdir(temp_dir)
        try:
            with mock.patch.object(
                database_manager, "DatabaseManager", _FakeDBManager
            ), mock.patch.object(
                llmbackend, "ArgoRAG", _FakeRAG
            ), mock.patch.object(
                explorer_api, "create_explorer_blueprint", _fake_blueprint
            ):
                if "ingestion" in sys.modules:
                    return importlib.reload(sys.modules["ingestion"])
                return importlib.import_module("ingestion")
        finally:
            os.chdir(original_cwd)


class RemediationEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion = load_ingestion_module()

    def setUp(self):
        self.client = self.ingestion.app.test_client()
        self.original_rag = self.ingestion.rag_chatbot

    def tearDown(self):
        self.ingestion.rag_chatbot = self.original_rag

    def test_compound_wmo_question_returns_a_deterministic_answer(self):
        engine = FakeEngine(rows=[])
        self.ingestion.rag_chatbot = make_rag(engine)

        response = self.client.post(
            "/get-question",
            json={"prompt": "Show profiles from WMO 1900121 collected after 2021."},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("response", body)
        self.assertIn("data", body)
        self.assertEqual(body["response"], "No profiles matched the requested filters.")
        self.assertEqual(body["data"], [])
        self.assertEqual(len(engine.calls), 1)

    def test_clarification_returns_200_with_no_rows(self):
        self.ingestion.rag_chatbot = make_rag(FakeEngine())

        response = self.client.post(
            "/get-question", json={"prompt": "Show profiles from cycle 99999."}
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("more detail", body["response"])
        self.assertEqual(body["data"], [])


if __name__ == "__main__":
    unittest.main()
