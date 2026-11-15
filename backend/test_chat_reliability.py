"""Phase 3B reliability regression tests for the Chat pipeline.

Covers:
  A. NULL / non-finite handling in ``_profile_summary`` and its callers.
  B. Database failures as distinct from successful empty results.
  C. Newest-first ordering for latest-profile queries.

No production database, ChromaDB or Ollama instance is used.
"""

import json
import unittest

from llmbackend import ArgoRAG, ChatRetrievalError


# ------------------------------------------------------------------
# Minimal fakes for the SQLAlchemy engine / connection / result API
# ------------------------------------------------------------------


class _Row(dict):
    """Row-like dict exposing the ``_mapping`` attribute the code uses."""

    @property
    def _mapping(self):
        return dict(self)


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if self._scalar is not None:
            return self._scalar
        return len(self._rows)


class _FakeConnection:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        sql = str(query)
        self._engine.calls.append((sql, dict(params or {})))

        if self._engine.failure:
            raise RuntimeError("simulated database outage")

        if "COUNT(*)" in sql:
            return _Result(scalar=self._engine.count)
        if "DISTINCT" in sql:
            return _Result(self._engine.wmo_rows)
        if "GROUP BY" in sql:
            return _Result(self._engine.group_rows)
        return _Result(self._engine.rows)


class _FakeEngine:
    def __init__(
        self,
        rows=None,
        count=None,
        wmo_rows=None,
        group_rows=None,
        failure=False,
    ):
        self.rows = rows or []
        self.count = count
        self.wmo_rows = wmo_rows or []
        self.group_rows = group_rows or []
        self.failure = failure
        self.calls = []

    def connect(self):
        return _FakeConnection(self)


class _FakeDBManager:
    def __init__(self, engine):
        self.mysql_engine = engine


def make_rag(engine):
    """Build an ArgoRAG without __init__ (no Ollama, no Chroma, no MySQL)."""
    rag = ArgoRAG.__new__(ArgoRAG)
    rag.db_manager = _FakeDBManager(engine)
    return rag


def make_profile(
    cycle=1,
    profile_time="2024-01-01 00:00:00",
    pressure="[0.0, 10.0, 20.0]",
    temperature="[20.0, 19.0, 18.0]",
    salinity="[35.0, 35.1, 35.2]",
    wmo=1900121,
    profile_id=1,
):
    return _Row(
        {
            "profile_id": profile_id,
            "wmo_number": wmo,
            "cycle_number": cycle,
            "profile_time": profile_time,
            "latitude": 15.0,
            "longitude": 65.0,
            "pressure": pressure,
            "temperature": temperature,
            "salinity": salinity,
            "bgc_params": None,
        }
    )


# ------------------------------------------------------------------
# A. NULL and non-finite handling
# ------------------------------------------------------------------


class ProfileSummaryNullTests(unittest.TestCase):
    def setUp(self):
        self.rag = make_rag(_FakeEngine())

    def summary(self, temperature):
        return self.rag._profile_summary(make_profile(temperature=temperature))

    def test_valid_numbers_only(self):
        summary = self.summary("[10.0, 20.0, 30.0]")

        self.assertEqual(summary["temperature_count"], 3)
        self.assertEqual(summary["temperature_valid_count"], 3)
        self.assertEqual(summary["temperature_missing_count"], 0)
        self.assertEqual(summary["temperature_min"], 10.0)
        self.assertEqual(summary["temperature_max"], 30.0)
        self.assertEqual(summary["temperature_mean"], 20.0)

    def test_numbers_with_none_use_valid_observations_only(self):
        summary = self.summary("[12.3, null, 14.5]")

        self.assertEqual(summary["temperature_count"], 3)
        self.assertEqual(summary["temperature_valid_count"], 2)
        self.assertEqual(summary["temperature_missing_count"], 1)
        self.assertEqual(summary["temperature_min"], 12.3)
        self.assertEqual(summary["temperature_max"], 14.5)
        self.assertEqual(summary["temperature_mean"], 13.4)

    def test_only_none_reports_unavailable_statistics(self):
        summary = self.summary("[null, null, null]")

        self.assertEqual(summary["temperature_count"], 3)
        self.assertEqual(summary["temperature_valid_count"], 0)
        self.assertEqual(summary["temperature_missing_count"], 3)
        self.assertIsNone(summary["temperature_min"])
        self.assertIsNone(summary["temperature_max"])
        self.assertIsNone(summary["temperature_mean"])
        # Missing data must never be represented as zero.
        self.assertNotEqual(summary["temperature_min"], 0)
        self.assertNotEqual(summary["temperature_mean"], 0)

    def test_empty_array(self):
        summary = self.summary("[]")

        self.assertEqual(summary["temperature_count"], 0)
        self.assertEqual(summary["temperature_valid_count"], 0)
        self.assertEqual(summary["temperature_missing_count"], 0)
        self.assertIsNone(summary["temperature_min"])
        self.assertIsNone(summary["temperature_max"])
        self.assertIsNone(summary["temperature_mean"])

    def test_nan_and_infinity_are_not_observations(self):
        summary = self.summary("[1.0, NaN, Infinity, -Infinity, 3.0]")

        self.assertEqual(summary["temperature_count"], 5)
        self.assertEqual(summary["temperature_valid_count"], 2)
        self.assertEqual(summary["temperature_missing_count"], 3)
        self.assertEqual(summary["temperature_min"], 1.0)
        self.assertEqual(summary["temperature_max"], 3.0)
        self.assertEqual(summary["temperature_mean"], 2.0)

    def test_non_numeric_values_are_skipped_not_zeroed(self):
        summary = self.rag._profile_summary(
            make_profile(temperature='["bad", null, 4.0]')
        )

        self.assertEqual(summary["temperature_valid_count"], 1)
        self.assertEqual(summary["temperature_min"], 4.0)
        self.assertEqual(summary["temperature_mean"], 4.0)

    def test_one_missing_profile_does_not_break_multi_profile_context(self):
        broken = make_profile(cycle=1, temperature="[null, null]", salinity="[]")
        valid = make_profile(cycle=2, temperature="[18.0, 19.0]")

        context = self.rag._build_compact_context([broken, valid])

        self.assertIn("Profile 1:", context)
        self.assertIn("Profile 2:", context)
        self.assertIn("No valid temperature measurements", context)
        self.assertIn("Temperature range: 18.0 to 19.0", context)
        self.assertNotIn("None to None", context)

    def test_direct_answer_reports_unavailable_instead_of_zero(self):
        answer = self.rag._build_direct_profile_answer(
            "Show me temperature measurements for WMO 1900121",
            [make_profile(temperature="[null, null]")],
        )

        self.assertIn("none holds a valid numeric observation", answer)
        self.assertNotIn("0 temperature", answer)
        self.assertNotIn("Temperature range", answer)

    def test_direct_answer_states_valid_and_missing_counts(self):
        answer = self.rag._build_direct_profile_answer(
            "Show me temperature measurements for WMO 1900121",
            [make_profile(temperature="[12.3, null, 14.5]")],
        )

        self.assertIn("2 valid temperature measurements", answer)
        self.assertIn("1 of 3 levels have no observation", answer)
        self.assertIn("12.3 to 14.5", answer)
        self.assertIn("mean 13.4", answer)


# ------------------------------------------------------------------
# B. Database error versus empty result
# ------------------------------------------------------------------


class RetrievalErrorTests(unittest.TestCase):
    def test_count_zero_records_is_a_successful_zero(self):
        rag = make_rag(_FakeEngine(count=0))

        self.assertEqual(rag._count_profiles_by_wmo(1900121), 0)

    def test_count_with_records_returns_the_count(self):
        rag = make_rag(_FakeEngine(count=7))

        self.assertEqual(rag._count_profiles_by_wmo(1900121), 7)

    def test_count_failure_raises_instead_of_returning_zero(self):
        rag = make_rag(_FakeEngine(failure=True))

        with self.assertRaises(ChatRetrievalError):
            rag._count_profiles_by_wmo(1900121)

    def test_available_floats_empty_list_is_empty_result(self):
        rag = make_rag(_FakeEngine(wmo_rows=[]))

        self.assertEqual(rag._retrieve_available_wmos(), [])

    def test_available_floats_success(self):
        rag = make_rag(_FakeEngine(wmo_rows=[(1900121,), (2900226,)]))

        self.assertEqual(
            rag._retrieve_available_wmos(), [1900121, 2900226]
        )

    def test_available_floats_failure_raises(self):
        rag = make_rag(_FakeEngine(failure=True))

        with self.assertRaises(ChatRetrievalError):
            rag._retrieve_available_wmos()

    def test_profile_counts_failure_raises(self):
        rag = make_rag(_FakeEngine(failure=True))

        with self.assertRaises(ChatRetrievalError):
            rag._retrieve_profile_counts_by_wmo()

    def test_profile_counts_empty_is_empty_result(self):
        rag = make_rag(_FakeEngine(group_rows=[]))

        self.assertEqual(rag._retrieve_profile_counts_by_wmo(), [])

    def test_retrieve_by_wmo_empty_is_empty_result(self):
        rag = make_rag(_FakeEngine(rows=[]))

        self.assertEqual(rag._retrieve_by_wmo(1900121, 5), [])

    def test_retrieve_by_wmo_failure_raises(self):
        rag = make_rag(_FakeEngine(failure=True))

        with self.assertRaises(ChatRetrievalError):
            rag._retrieve_by_wmo(1900121, 5)

    def test_pipeline_empty_result_is_not_an_error(self):
        rag = make_rag(_FakeEngine(wmo_rows=[]))

        answer, data = rag.answer_question("How many floats are available?")

        self.assertEqual(data, [])
        self.assertIn("No ARGO float IDs", answer)

    def test_pipeline_failure_raises_for_the_api_layer(self):
        rag = make_rag(_FakeEngine(failure=True))

        with self.assertRaises(ChatRetrievalError):
            rag.answer_question("How many floats are available?")


# ------------------------------------------------------------------
# C. Latest-profile ordering
# ------------------------------------------------------------------


class LatestOrderingTests(unittest.TestCase):
    def test_latest_detection(self):
        detect = ArgoRAG._is_latest_request

        self.assertTrue(detect("Show me the latest profile for WMO 1900121"))
        self.assertTrue(detect("What is the most recent profile?"))
        self.assertTrue(detect("Give me the newest cycle"))
        self.assertFalse(detect("List all profiles for WMO 1900121"))
        self.assertFalse(detect("Show temperature for WMO 1900121"))

    def test_newest_first_uses_descending_sql_with_nulls_last(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 1, newest_first=True)

        sql = engine.calls[0][0]
        self.assertIn(
            "ORDER BY (p.profile_time IS NULL), p.profile_time DESC", sql
        )
        self.assertNotIn("p.profile_time ASC", sql)

    def test_default_order_remains_chronological(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 5)

        sql = engine.calls[0][0]
        self.assertIn(
            "ORDER BY (p.profile_time IS NULL), p.profile_time ASC", sql
        )

    def test_limit_remains_a_bound_parameter(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 2, newest_first=True)

        sql, params = engine.calls[0]
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual(params["limit"], 2)
        self.assertEqual(params["wmo_number"], 1900121)

    def test_newest_row_is_returned_first(self):
        rows = [
            make_profile(cycle=30, profile_time="2024-03-01 00:00:00"),
            make_profile(cycle=29, profile_time="2024-02-01 00:00:00"),
            make_profile(cycle=28, profile_time="2024-01-01 00:00:00"),
        ]
        rag = make_rag(_FakeEngine(rows=rows))

        context = rag._retrieve_by_wmo(1900121, 3, newest_first=True)

        self.assertEqual(
            [profile["cycle_number"] for profile in context], [30, 29, 28]
        )

    def test_equal_timestamps_preserve_database_order(self):
        rows = [
            make_profile(cycle=5, profile_time="2024-01-01 00:00:00"),
            make_profile(cycle=6, profile_time="2024-01-01 00:00:00"),
        ]
        rag = make_rag(_FakeEngine(rows=rows))

        context = rag._retrieve_by_wmo(1900121, 2, newest_first=True)

        # No Python-side re-sorting: the database ordering is authoritative.
        self.assertEqual([row["cycle_number"] for row in context], [5, 6])

    def test_null_timestamps_sort_last(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 1, newest_first=True)

        # The NULL guard is part of the SQL order, so an undated profile can
        # never be presented as the newest observation.
        self.assertIn("(p.profile_time IS NULL)", engine.calls[0][0])

    def test_fewer_records_than_limit(self):
        engine = _FakeEngine(rows=[make_profile(cycle=3)])
        rag = make_rag(engine)

        answer, data = rag.answer_question(
            "Show me the latest profile for WMO 1900121"
        )

        self.assertEqual(len(data), 1)
        self.assertIn("most recent one", answer)

    def test_more_records_than_limit_returns_only_the_limit(self):
        # The fake connection simulates a database that honours LIMIT.
        engine = _FakeEngine(
            count=10,
            rows=[
                make_profile(cycle=10, profile_time="2024-03-01 00:00:00"),
                make_profile(cycle=9, profile_time="2024-02-01 00:00:00"),
            ],
        )
        rag = make_rag(engine)

        answer, data = rag.answer_question(
            "Show the 2 latest profiles for WMO 1900121"
        )

        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["cycle_number"], 10)
        self.assertIn("10 profiles", answer)
        self.assertIn("newest first", answer)

        limit_params = [
            params.get("limit")
            for sql, params in engine.calls
            if "LIMIT :limit" in sql
        ]
        self.assertEqual(limit_params, [2])


class LatestTieBreakTests(unittest.TestCase):
    def test_newest_first_breaks_timestamp_ties_by_cycle(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 1, newest_first=True)

        self.assertIn(
            "p.profile_time DESC, p.cycle_number DESC", engine.calls[0][0]
        )

    def test_ascending_order_breaks_timestamp_ties_by_cycle(self):
        engine = _FakeEngine(rows=[make_profile()])
        rag = make_rag(engine)

        rag._retrieve_by_wmo(1900121, 5)

        self.assertIn(
            "p.profile_time ASC, p.cycle_number ASC", engine.calls[0][0]
        )


# ------------------------------------------------------------------
# D. Semantic retrieval: empty result versus retrieval failure
# ------------------------------------------------------------------


class _Vector:
    def tolist(self):
        return [0.0, 1.0]


class _FakeEmbedding:
    def encode(self, text):
        return _Vector()


class _FakeCollection:
    def __init__(self, ids=None, metadatas=None, error=False):
        self._ids = [[]] if ids is None else ids
        self._metadatas = [[]] if metadatas is None else metadatas
        self.error = error

    def query(self, query_embeddings, n_results):
        if self.error:
            raise RuntimeError("simulated chroma outage")
        return {"ids": self._ids, "metadatas": self._metadatas}


class _SemanticDBManager:
    def __init__(self, collection, engine):
        self.embedding_model = _FakeEmbedding()
        self.chroma_collection = collection
        self.mysql_engine = engine


def make_semantic_rag(collection, engine=None):
    rag = ArgoRAG.__new__(ArgoRAG)
    rag.db_manager = _SemanticDBManager(collection, engine or _FakeEngine())
    return rag


class SemanticRetrievalTests(unittest.TestCase):
    def test_empty_collection_is_a_valid_empty_result(self):
        rag = make_semantic_rag(_FakeCollection(ids=[[]]))

        self.assertEqual(rag._retrieve_semantic("some question"), [])

    def test_profiles_without_sql_ids_are_a_valid_empty_result(self):
        rag = make_semantic_rag(
            _FakeCollection(ids=[["doc-1"]], metadatas=[[{}]])
        )

        self.assertEqual(rag._retrieve_semantic("some question"), [])

    def test_chroma_exception_is_a_retrieval_failure(self):
        rag = make_semantic_rag(_FakeCollection(error=True))

        with self.assertRaises(ChatRetrievalError):
            rag._retrieve_semantic("some question")

    def test_mysql_exception_is_a_retrieval_failure(self):
        rag = make_semantic_rag(
            _FakeCollection(
                ids=[["doc-1"]],
                metadatas=[[{"profile_id_sql": 5}]],
            ),
            _FakeEngine(failure=True),
        )

        with self.assertRaises(ChatRetrievalError):
            rag._retrieve_semantic("some question")


if __name__ == "__main__":
    unittest.main()
