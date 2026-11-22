"""Phase 4B integration tests: QuerySpec pipeline inside the chat workflow.

These tests exercise the real ``ArgoRAG.answer_question`` dispatch and the real
``/get-question`` Flask route.  MySQL, ChromaDB and Ollama are replaced by
in-memory fakes, so no external service is contacted.

Covered here:

* a supported structured question reaches the deterministic pipeline and is
  executed exactly once;
* unsupported plans fall back to the existing semantic + Qwen workflow without
  a second database execution;
* ambiguous and invalid requests are reported explicitly and execute nothing;
* an execution failure is a retrieval error, never an empty answer;
* the existing WMO route and the existing ChromaDB route still work;
* the route still returns ``{"response": ..., "data": [...]}``.
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
from llmbackend import ArgoRAG, ChatRetrievalError
from query_integration import (
    DeterministicAnswer,
    DeterministicPipelineError,
    answer_deterministically,
)


# ------------------------------------------------------------------
# Fake SQLAlchemy engine
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
        if self._engine.failure:
            raise RuntimeError("simulated database failure")
        if self._engine.scalar is not None and "COUNT(*)" in sql:
            return _Result(scalar=self._engine.scalar)
        return _Result(self._engine.rows)


class FakeEngine:
    def __init__(self, rows=(), scalar=None, failure=False):
        self.rows = list(rows)
        self.scalar = scalar
        self.failure = failure
        self.calls = []

    def connect(self):
        return _Connection(self)


def make_rag(engine):
    """Build a real ArgoRAG without touching Ollama, ChromaDB or MySQL."""

    rag = ArgoRAG.__new__(ArgoRAG)
    rag.db_manager = mock.Mock(mysql_engine=engine)
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


LIST_QUERY = "Show salinity profiles between 2019-01-01 and 2019-12-31"
DEPTH_QUERY = "Show temperature at 500 metres depth"
INVALID_QUERY = "Show profiles between 2020-06-01 and 2020-01-01"
PRESSURE_QUERY = "Show temperature profiles with pressure between 0 and 500 dbar"
SEMANTIC_QUERY = "Show me some profiles"


# ------------------------------------------------------------------
# Chat workflow integration
# ------------------------------------------------------------------


class DeterministicDispatchTests(unittest.TestCase):
    def test_supported_query_is_executed_once_and_skips_semantic_search(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question(LIST_QUERY)

        self.assertIn("Found 1 profile", answer)
        self.assertEqual([row["profile_id"] for row in data], [1])
        self.assertEqual(len(engine.calls), 1)
        self.assertIn("LIMIT :limit", engine.calls[0][0])
        rag._retrieve_semantic.assert_not_called()

    def test_measurement_arrays_are_not_forwarded_to_the_frontend(self):
        row = dict(profile_row())
        row["pressure"] = [0.0, 10.0, 20.0]
        row["temperature"] = [20.0, 19.0, 18.0]
        engine = FakeEngine(rows=[row])
        rag = make_rag(engine)

        _, data = rag.answer_question(LIST_QUERY)

        self.assertNotIn("pressure", data[0])
        self.assertNotIn("temperature", data[0])
        self.assertEqual(data[0]["wmo_number"], 1900121)

    def test_count_question_returns_a_deterministic_count(self):
        engine = FakeEngine(rows=[{"profile_count": 7}])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question(
            "How many profiles were collected in 2020?"
        )

        self.assertEqual(answer, "There are 7 profiles matching the request.")
        self.assertEqual(data, [])
        self.assertEqual(len(engine.calls), 1)

    def test_ambiguous_query_returns_a_clarification_and_executes_nothing(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question(DEPTH_QUERY)

        self.assertIn("more detail", answer)
        self.assertIn("pressure", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])
        rag._retrieve_semantic.assert_not_called()

    def test_invalid_request_is_explicit_and_executes_nothing(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question(INVALID_QUERY)

        self.assertIn("could not run that query", answer)
        self.assertIn("No data was retrieved", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])
        rag._retrieve_semantic.assert_not_called()

    def test_unsupported_structured_constraint_is_refused(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(
            side_effect=AssertionError("unsupported constraint must not fall back")
        )

        answer, data = rag.answer_question(PRESSURE_QUERY)

        # Phase 4B.2: a pressure range cannot be executed, so the answer says
        # so instead of returning profiles as though the filter were applied.
        self.assertIn("cannot apply", answer)
        self.assertIn("Pressure filtering", answer)
        self.assertEqual(data, [])
        self.assertEqual(engine.calls, [])
        rag._retrieve_semantic.assert_not_called()

    def test_semantic_questions_still_use_chromadb_and_qwen(self):
        engine = FakeEngine(rows=[])
        rag = make_rag(engine)
        context = [profile_row()]
        rag._retrieve_semantic = mock.Mock(return_value=context)

        with mock.patch.object(
            llmbackend.ollama, "generate", return_value={"response": "Generated."}
        ):
            answer, data = rag.answer_question(SEMANTIC_QUERY)

        self.assertEqual(answer, "Generated.")
        self.assertEqual(data, context)
        rag._retrieve_semantic.assert_called_once_with(SEMANTIC_QUERY)
        self.assertEqual(engine.calls, [])

    def test_existing_wmo_route_is_unchanged(self):
        engine = FakeEngine(scalar=12)
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question("How many profiles for WMO 1900121?")

        self.assertEqual(answer, "WMO 1900121 contains 12 profiles.")
        self.assertEqual(data, [])
        self.assertEqual(len(engine.calls), 1)
        self.assertIn("COUNT(*)", engine.calls[0][0])
        self.assertEqual(engine.calls[0][1], {"wmo_number": 1900121})

    def test_pipeline_execution_failure_is_a_retrieval_error(self):
        rag = make_rag(FakeEngine(failure=True))
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        with self.assertRaises(ChatRetrievalError):
            rag.answer_question(LIST_QUERY)

        rag._retrieve_semantic.assert_not_called()

    def test_execution_failure_is_not_an_empty_success(self):
        with self.assertRaises(DeterministicPipelineError):
            answer_deterministically(LIST_QUERY, FakeEngine(failure=True))

    def test_empty_result_is_a_valid_executed_answer(self):
        rag = make_rag(FakeEngine(rows=[]))
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))

        answer, data = rag.answer_question(LIST_QUERY)

        self.assertEqual(answer, "No profiles matched the requested filters.")
        self.assertEqual(data, [])

    def test_unrelated_question_is_not_handled_by_the_pipeline(self):
        outcome = answer_deterministically("hello", FakeEngine(rows=[]))

        self.assertIsNone(outcome)
        self.assertIsNone(
            answer_deterministically("Compare WMO 1900121 and 2900226", FakeEngine())
        )

    def test_renderer_returns_deterministic_answer_objects(self):
        outcome = answer_deterministically(
            "How many profiles were collected in 2020?",
            FakeEngine(rows=[{"profile_count": 1}]),
        )

        self.assertIsInstance(outcome, DeterministicAnswer)
        self.assertEqual(outcome.status, "executed")
        self.assertIn("1 profile", outcome.answer)


# ------------------------------------------------------------------
# Real Flask route (no database, ChromaDB or Ollama)
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
    return flask.Blueprint("fake_explorer_phase4b", __name__)


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


class GetQuestionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion = load_ingestion_module()

    def setUp(self):
        self.client = self.ingestion.app.test_client()
        self.original_rag = self.ingestion.rag_chatbot

    def tearDown(self):
        self.ingestion.rag_chatbot = self.original_rag

    def test_structured_query_uses_pipeline_and_keeps_response_contract(self):
        engine = FakeEngine(rows=[profile_row()])
        rag = make_rag(engine)
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))
        self.ingestion.rag_chatbot = rag

        response = self.client.post("/get-question", json={"prompt": LIST_QUERY})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("response", body)
        self.assertIn("data", body)
        self.assertIn("Found 1 profile", body["response"])
        self.assertEqual(body["data"][0]["profile_id"], 1)
        self.assertEqual(len(engine.calls), 1)

    def test_execution_failure_returns_503_not_a_fabricated_answer(self):
        rag = make_rag(FakeEngine(failure=True))
        rag._retrieve_semantic = mock.Mock(side_effect=AssertionError("no Chroma"))
        self.ingestion.rag_chatbot = rag

        response = self.client.post("/get-question", json={"prompt": LIST_QUERY})

        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertIn("retrieval failure", body["error"])
        self.assertNotIn("data", body)


if __name__ == "__main__":
    unittest.main()
