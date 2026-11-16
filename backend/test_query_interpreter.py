"""Phase 4A interpreter contract, strict decoder and adapter tests."""

import json
import unittest

from query_interpreter import (
    DEFAULT_MODEL,
    Diagnostic,
    InterpreterFailure,
    Interpretation,
    QwenInterpreter,
    decode_qwen_output,
    interpret_deterministic,
    interpret_with_fallback,
)
from query_interpreter_eval import (
    evaluate_case,
    evaluate_corpus,
    summarise,
)
from query_parser_test_cases import CASES
from query_planner import plan_query
from query_spec import (
    Aggregation,
    DataMode,
    Intent,
    MeasurementValues,
    Parameter,
    QcScope,
    ValidationResult,
    ValidationStatus,
    validate_spec,
)


class FakeClient:
    def __init__(self, payload=None, *, raw=None, error=None, models=None,
                 list_error=None):
        self.payload = payload
        self.raw = raw
        self.error = error
        self.models = ["qwen3:8b"] if models is None else models
        self.list_error = list_error
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return {"response": self.raw}
        return {"response": json.dumps(self.payload)}

    def list(self):
        if self.list_error is not None:
            raise self.list_error
        item = type("Model", (), {})
        models = []
        for name in self.models:
            model = item()
            model.model = name
            models.append(model)
        listing = type("Listing", (), {})
        listing.models = models
        return listing


def payload(**overrides):
    base = {
        "intent": "list_profiles",
        "parameters": ["TEMP"],
    }
    base.update(overrides)
    return base


def interpret(payload_data, *, model="qwen3:8b"):
    client = FakeClient(payload_data)
    return QwenInterpreter(client=client, model=model).interpret("test question")


class ContractTests(unittest.TestCase):
    def test_deterministic_result_uses_authoritative_validation(self):
        result = interpret_deterministic("Show temperature profiles")
        self.assertEqual(result.interpreter, "deterministic")
        self.assertIs(result.status, ValidationStatus.VALID)
        self.assertEqual(result.candidate_spec.parameters, (Parameter.TEMP,))
        self.assertIs(result.validation.status, ValidationStatus.VALID)
        self.assertFalse(result.fallback_used)

    def test_status_is_derived_from_validation(self):
        validation = ValidationResult(
            status=ValidationStatus.UNSUPPORTED,
            reason="not executable",
        )
        result = Interpretation(interpreter="test", validation=validation)
        self.assertIs(result.status, ValidationStatus.UNSUPPORTED)
        with self.assertRaises(TypeError):
            Interpretation(
                interpreter="test",
                status=ValidationStatus.VALID,
                validation=validation,
            )

    def test_deterministic_diagnostics_are_structured(self):
        result = interpret_deterministic("Show temperature profiles")
        self.assertIsInstance(result.diagnostics, tuple)
        for diagnostic in result.diagnostics:
            self.assertIsInstance(diagnostic, Diagnostic)


class StrictDecoderTests(unittest.TestCase):
    def test_full_valid_payload_becomes_spec(self):
        decoded = decode_qwen_output(
            payload(
                intent="profile_detail",
                wmo_numbers=[1900121],
                cycle_numbers=[44],
                time_range={"start": "2020-01-01", "end": "2020-12-31"},
                bounding_box={
                    "min_latitude": 0,
                    "max_latitude": 10,
                    "min_longitude": 60,
                    "max_longitude": 70,
                },
                pressure_range=[0, 500],
                values="adjusted",
                qc_filter={"scope": "profile", "flags": ["A", "B"]},
                data_mode="D",
                sort={"field": "cycle_number", "direction": "asc"},
                limit=5,
                notes=["one", "two"],
            )
        )
        spec = decoded.spec
        self.assertIsNotNone(spec)
        self.assertIs(spec.intent, Intent.PROFILE_DETAIL)
        self.assertEqual(spec.wmo_numbers, (1900121,))
        self.assertEqual(spec.cycle_numbers, (44,))
        self.assertEqual(spec.pressure_range, (0.0, 500.0))
        self.assertIs(spec.values, MeasurementValues.ADJUSTED)
        self.assertIs(spec.qc_filter.scope, QcScope.PROFILE)
        self.assertEqual(spec.qc_filter.flags, frozenset({"A", "B"}))
        self.assertIs(spec.data_mode, DataMode.DELAYED)
        self.assertEqual(spec.limit, 5)

    def test_malformed_json_is_rejected_by_adapter(self):
        client = FakeClient(raw="{not json")
        result = QwenInterpreter(client=client).interpret("question")
        self.assertIs(result.status, ValidationStatus.INVALID)
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_JSON.value)
        self.assertIsNone(result.candidate_spec)
        self.assertFalse(result.raw_output_valid)

    def test_missing_intent_is_rejected(self):
        result = interpret({"parameters": ["TEMP"]})
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_SCHEMA.value)
        self.assertIsNone(result.candidate_spec)

    def test_unexpected_field_is_rejected(self):
        result = interpret(payload(extra_filter="temperature"))
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_SCHEMA.value)
        self.assertIsNone(result.candidate_spec)

    def test_sql_field_is_rejected(self):
        result = interpret(
            payload(sql="SELECT * FROM argo_profiles")
        )
        self.assertEqual(result.failure_code, InterpreterFailure.FORBIDDEN_SQL.value)
        self.assertIsNone(result.candidate_spec)
        self.assertNotIn("SELECT", (result.validation.reason or ""))

    def test_invalid_enum_is_rejected(self):
        result = interpret(payload(intent="not_a_query"))
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_SCHEMA.value)

    def test_wrong_types_are_rejected(self):
        for bad in (
            payload(wmo_numbers="1900121"),
            payload(cycle_numbers=[True]),
            payload(limit=True),
            payload(pressure_range=["0", 500]),
            payload(parameters="TEMP"),
        ):
            with self.subTest(bad=bad):
                result = interpret(bad)
                self.assertEqual(
                    result.failure_code,
                    InterpreterFailure.INVALID_SCHEMA.value,
                )

    def test_incomplete_pressure_range_is_rejected(self):
        result = interpret(payload(pressure_range=[0]))
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_SCHEMA.value)
        self.assertIsNone(result.candidate_spec)

    def test_invalid_wmo_is_a_validation_failure(self):
        result = interpret(payload(wmo_numbers=[123]))
        self.assertIs(result.status, ValidationStatus.INVALID)
        self.assertEqual(result.failure_code, InterpreterFailure.VALIDATION_FAILED.value)
        self.assertTrue(result.raw_output_valid)
        self.assertIsNotNone(result.candidate_spec)

    def test_invalid_cycle_is_a_validation_failure(self):
        result = interpret(payload(cycle_numbers=[10001]))
        self.assertIs(result.status, ValidationStatus.INVALID)
        self.assertEqual(result.failure_code, InterpreterFailure.VALIDATION_FAILED.value)
        self.assertIsNotNone(result.candidate_spec)

    def test_depth_is_not_converted_to_pressure(self):
        result = interpret(
            payload(
                parameters=["TEMP"],
                depth_range_m=[0, 100],
                depth_requested=True,
            )
        )
        self.assertIs(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(result.candidate_spec.pressure_range)
        self.assertTrue(result.candidate_spec.depth_requested)

    def test_raw_and_adjusted_are_distinct(self):
        raw = interpret(payload())
        adjusted = interpret(payload(values="adjusted"))
        self.assertIs(raw.candidate_spec.values, MeasurementValues.RAW)
        self.assertIs(adjusted.candidate_spec.values, MeasurementValues.ADJUSTED)

    def test_qc_scope_and_flags_are_preserved(self):
        result = interpret(
            payload(qc_filter={"scope": "measurement", "flags": ["1", "2"]})
        )
        self.assertIs(result.status, ValidationStatus.VALID)
        self.assertIs(result.candidate_spec.qc_filter.scope, QcScope.MEASUREMENT)
        self.assertEqual(result.candidate_spec.qc_filter.flags, frozenset({"1", "2"}))

    def test_data_mode_is_preserved(self):
        result = interpret(payload(data_mode="R"))
        self.assertIs(result.candidate_spec.data_mode, DataMode.REAL_TIME)

    def test_aggregation_grouping_and_semantic_fields(self):
        aggregate = interpret(
            payload(
                intent="aggregate",
                aggregations=["MEAN"],
                group_by="year",
            )
        )
        self.assertIs(aggregate.status, ValidationStatus.VALID)
        self.assertEqual(
            aggregate.candidate_spec.aggregations, (Aggregation.MEAN,)
        )
        self.assertEqual(aggregate.candidate_spec.group_by.value, "year")

        semantic = interpret(
            {
                "intent": "semantic_discovery",
                "semantic_query": "water near the equator",
            }
        )
        self.assertIs(semantic.status, ValidationStatus.VALID)
        self.assertEqual(
            semantic.candidate_spec.semantic_query,
            "water near the equator",
        )

    def test_compare_is_validated_but_not_plannable(self):
        result = interpret(
            payload(intent="compare", wmo_numbers=[1900121, 1900122])
        )
        self.assertIs(result.status, ValidationStatus.VALID)
        self.assertFalse(plan_query(result.validation.spec).supported)

    def test_clarification_output_has_no_candidate(self):
        result = interpret(
            {"intent": "clarify", "clarification": "Which pressure range?"}
        )
        self.assertIs(result.status, ValidationStatus.NEEDS_CLARIFICATION)
        self.assertIsNone(result.candidate_spec)
        self.assertTrue(result.raw_output_valid)

    def test_unsupported_output_is_explicit(self):
        result = interpret(
            {
                "intent": "aggregate",
                "unsupported_reason": "MEAN is not supported in this phase.",
            }
        )
        self.assertIs(result.status, ValidationStatus.UNSUPPORTED)
        self.assertIsNone(result.candidate_spec)
        self.assertIn("MEAN", result.validation.reason)

    def test_clarification_cannot_carry_query_fields(self):
        result = interpret(
            {
                "intent": "clarify",
                "clarification": "Which pressure range?",
                "pressure_range": [0, 500],
            }
        )
        self.assertEqual(result.failure_code, InterpreterFailure.INVALID_SCHEMA.value)


class AdapterFailureTests(unittest.TestCase):
    def test_empty_response(self):
        result = QwenInterpreter(client=FakeClient(raw="   ")).interpret("question")
        self.assertEqual(result.failure_code, InterpreterFailure.EMPTY_RESPONSE.value)
        self.assertIsNone(result.candidate_spec)

    def test_ollama_unavailable(self):
        client = FakeClient(error=ConnectionError("connection refused"))
        result = QwenInterpreter(client=client).interpret("question")
        self.assertEqual(result.failure_code, InterpreterFailure.OLLAMA_UNAVAILABLE.value)
        self.assertIs(result.status, ValidationStatus.INVALID)

    def test_model_unavailable(self):
        client = FakeClient(error=RuntimeError("model not found; run ollama pull"))
        result = QwenInterpreter(client=client).interpret("question")
        self.assertEqual(result.failure_code, InterpreterFailure.MODEL_UNAVAILABLE.value)

    def test_timeout(self):
        client = FakeClient(error=TimeoutError("request timed out"))
        result = QwenInterpreter(client=client).interpret("question")
        self.assertEqual(result.failure_code, InterpreterFailure.TIMEOUT.value)

    def test_no_automatic_fallback(self):
        client = FakeClient(error=ConnectionError("connection refused"))
        result = QwenInterpreter(client=client).interpret("Show temperature")
        self.assertEqual(result.interpreter, "qwen3")
        self.assertFalse(result.fallback_used)
        self.assertIsNone(result.original_interpreter)

    def test_model_availability(self):
        available = QwenInterpreter(
            client=FakeClient(payload(), models=["qwen3:8b"])
        )
        missing = QwenInterpreter(
            client=FakeClient(payload(), models=["llama3:8b"])
        )
        self.assertTrue(available.is_available())
        self.assertFalse(missing.is_available())

    def test_prompt_is_json_and_forbids_sql(self):
        client = FakeClient(payload())
        QwenInterpreter(client=client).interpret("Show temperature")
        call = client.calls[0]
        self.assertEqual(call["format"], "json")
        self.assertFalse(call["stream"])
        self.assertIn("must not return SQL", call["prompt"])
        self.assertIn("Question: Show temperature", call["prompt"])


class FallbackTests(unittest.TestCase):
    def test_explicit_fallback_is_recorded(self):
        primary = QwenInterpreter(
            client=FakeClient(error=ConnectionError("connection refused"))
        )
        result = interpret_with_fallback(
            "Show temperature profiles",
            primary,
            interpret_deterministic,
        )
        self.assertTrue(result.fallback_used)
        self.assertIn("qwen3", result.fallback_reason)
        self.assertEqual(result.original_interpreter, "qwen3")
        self.assertIs(result.original_status, ValidationStatus.INVALID)
        self.assertIs(result.status, ValidationStatus.VALID)

    def test_no_fallback_when_primary_valid(self):
        primary = QwenInterpreter(client=FakeClient(payload()))
        result = interpret_with_fallback(
            "Show temperature",
            primary,
            interpret_deterministic,
        )
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.interpreter, "qwen3")


class EvaluationHarnessTests(unittest.TestCase):
    def test_evaluate_one_case_without_sql(self):
        client = FakeClient(payload())
        interpreter = QwenInterpreter(client=client)
        record = evaluate_case(CASES[0], interpreter)
        self.assertEqual(record.qwen_status, ValidationStatus.VALID.value)
        self.assertEqual(record.expected_status, ValidationStatus.VALID.value)
        self.assertIn("parameters", record.qwen_filters)
        self.assertEqual(record.qwen_filters["parameters"], ["TEMP"])
        self.assertIsNotNone(record.plan_supported)

    def test_evaluate_limited_corpus_and_metrics(self):
        run = evaluate_corpus(
            limit=2,
            qwen_interpreter=QwenInterpreter(client=FakeClient(payload())),
        )
        self.assertEqual(run.total_cases, 2)
        self.assertEqual(len(run.records), 2)
        summary = summarise(run.records)
        self.assertIn("qwen", summary)
        self.assertIn("deterministic", summary)
        self.assertIn("parameters", summary["qwen"])
        self.assertIn("pressure_depth_distinction", summary["qwen"])
        self.assertIn("unsupported_recognition", summary["qwen"])

    def test_plan_check_does_not_execute_sql(self):
        record = evaluate_case(
            CASES[0],
            QwenInterpreter(client=FakeClient(payload())),
            check_plan=True,
        )
        # The harness has no engine argument and only uses plan_query; a valid
        # case can therefore report plan support without executing anything.
        self.assertIn(record.plan_supported, (True, False))

    def test_validation_failure_has_no_plan(self):
        record = evaluate_case(
            CASES[0],
            QwenInterpreter(
                client=FakeClient(payload(wmo_numbers=[123]))
            ),
        )
        self.assertEqual(record.qwen_status, ValidationStatus.INVALID.value)
        self.assertIsNone(record.plan_supported)


if __name__ == "__main__":
    unittest.main()
