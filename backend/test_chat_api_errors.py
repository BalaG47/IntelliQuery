"""Isolated Flask API tests for the /get-question route (Phase 3B follow-up).

`ingestion.py` initializes the production database, ChromaDB and Ollama at
import time. To test the route without any of that, the app is imported with
its initializer dependencies replaced by in-memory fakes and with the working
directory pointed at a temporary folder (the module creates a ``data``
directory). Nothing in the application is refactored.

No MySQL, ChromaDB or Ollama connection is created by these tests.
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
from llmbackend import ChatRetrievalError


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
    return flask.Blueprint("fake_explorer", __name__)


def load_ingestion_module():
    """Import ingestion.py with its production initializers patched out."""

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


class GetQuestionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion = load_ingestion_module()

    def setUp(self):
        self.client = self.ingestion.app.test_client()
        self.original_pipeline = self.ingestion.rag_chatbot
        self.pipeline = mock.Mock()
        self.ingestion.rag_chatbot = self.pipeline

    def tearDown(self):
        self.ingestion.rag_chatbot = self.original_pipeline

    def post(self, payload):
        return self.client.post("/get-question", json=payload)

    def test_successful_query_returns_200(self):
        self.pipeline.answer_question.return_value = (
            "WMO 1900121 has 63 profiles.",
            [{"profile_id": 1}],
        )

        response = self.post({"prompt": "How many profiles for WMO 1900121?"})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"], [{"profile_id": 1}])
        self.assertIn("63 profiles", body["response"])

    def test_valid_empty_result_returns_200(self):
        self.pipeline.answer_question.return_value = (
            "I couldn't find any profiles for WMO number 1900121.",
            [],
        )

        response = self.post({"prompt": "How many floats are available?"})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"], [])
        self.assertEqual(body["response"], (
            "I couldn't find any profiles for WMO number 1900121."
        ))
        self.assertNotIn("error", body)

    def test_retrieval_failure_returns_503(self):
        self.pipeline.answer_question.side_effect = ChatRetrievalError(
            "Profiles for WMO 1900121 could not be retrieved."
        )

        response = self.post({"prompt": "Show profiles for WMO 1900121"})

        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertIn("could not complete this query", body["error"])
        self.assertIn("retrieval failure", body["error"])
        self.assertNotIn("response", body)
        # Internal detail from the exception must not reach the client.
        self.assertNotIn("1900121", body["error"])

    def test_unexpected_exception_returns_500_without_leaking_details(self):
        self.pipeline.answer_question.side_effect = RuntimeError(
            "secret-password at C:/internal/path"
        )

        response = self.post({"prompt": "Show profiles for WMO 1900121"})

        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertIn("internal error", body["error"])
        self.assertNotIn("secret-password", body["error"])
        self.assertNotIn("internal/path", body["error"])

    def test_missing_prompt_returns_400(self):
        response = self.post({"unrelated": "value"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "No prompt was provided."
        )
        self.pipeline.answer_question.assert_not_called()

    def test_empty_body_returns_400(self):
        response = self.post({})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "Request body is empty."
        )
        self.pipeline.answer_question.assert_not_called()


if __name__ == "__main__":
    unittest.main()
