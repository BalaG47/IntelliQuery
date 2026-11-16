"""Phase 4A interpretation evaluation harness.

Compares Qwen3, the deterministic parser and the existing corpus expectations.
This module only validates and optionally plans; it never executes SQL and never
accepts a SQL string from a model.

The live Ollama call is explicit (``--limit`` or ``--all``).  If Ollama is not
available, the CLI reports that limitation instead of fabricating scores.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable

from query_interpreter import (
    PROMPT_VERSION,
    QwenInterpreter,
    interpret_deterministic,
)
from query_parser_test_cases import CASES, ParserCase
from query_planner import plan_query
from query_spec import ValidationStatus


@dataclass(frozen=True)
class EvaluationRecord:
    case_id: str
    query: str
    category: str
    expected_intent: str
    expected_status: str
    expected_filters: dict
    qwen_intent: str | None
    qwen_status: str
    qwen_filters: dict
    qwen_raw_output_valid: bool | None
    qwen_failure_code: str | None
    deterministic_intent: str | None
    deterministic_status: str
    deterministic_filters: dict
    validation_outcome: str | None
    field_differences: tuple[str, ...]
    deterministic_field_differences: tuple[str, ...]
    clarification_correct: bool | None
    plan_supported: bool | None


@dataclass(frozen=True)
class EvaluationRun:
    model_name: str
    prompt_version: str
    generation_options: dict
    total_cases: int
    records: tuple[EvaluationRecord, ...]


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (frozenset, set)):
        return sorted(_json_value(item) for item in value)
    return value


def _expected_fields(case: ParserCase) -> dict:
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
        "sort_direction": case.sort_direction,
        "has_time": case.has_time_range,
        "has_bbox": case.has_bbox,
    }


def _actual_fields(spec) -> dict:
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
        "sort_direction": spec.sort.direction,
        "has_time": spec.time_range is not None,
        "has_bbox": spec.bounding_box is not None,
    }


def _spec_fields(interpretation) -> dict:
    spec = interpretation.candidate_spec
    if spec is None:
        spec = interpretation.validation.spec
    return _actual_fields(spec)


def _intent(interpretation) -> str | None:
    spec = interpretation.candidate_spec or interpretation.validation.spec
    return spec.intent.value if spec is not None else None


def _field_differences(expected: dict, actual: dict) -> tuple[str, ...]:
    differences = []
    for field, expected_value in expected.items():
        if expected_value is None or expected_value == ():
            continue
        if actual.get(field) != expected_value:
            differences.append(field)
    return tuple(differences)


def _case_id(case: ParserCase) -> str:
    return case.label


def evaluate_case(
    case: ParserCase,
    qwen_interpreter,
    *,
    check_plan: bool = True,
) -> EvaluationRecord:
    """Evaluate one corpus case without executing SQL."""

    deterministic = interpret_deterministic(case.query)
    qwen = qwen_interpreter.interpret(case.query)

    qwen_intent = _intent(qwen)
    qwen_filters = _spec_fields(qwen)
    deterministic_intent = _intent(deterministic)
    deterministic_filters = _spec_fields(deterministic)
    expected_filters = _expected_fields(case)
    differences = _field_differences(expected_filters, qwen_filters)
    deterministic_differences = _field_differences(
        expected_filters, deterministic_filters
    )

    clarification_correct = None
    if case.status is ValidationStatus.NEEDS_CLARIFICATION:
        clarification_correct = qwen.status is ValidationStatus.NEEDS_CLARIFICATION

    plan_supported = None
    if check_plan and qwen.status is ValidationStatus.VALID:
        spec = qwen.validation.spec or qwen.candidate_spec
        if spec is not None:
            plan_supported = plan_query(spec).supported

    return EvaluationRecord(
        case_id=_case_id(case),
        query=case.query,
        category=case.category,
        expected_intent=case.intent.value,
        expected_status=case.status.value,
        expected_filters=_json_value(expected_filters),
        qwen_intent=qwen_intent,
        qwen_status=qwen.status.value,
        qwen_filters=_json_value(qwen_filters),
        qwen_raw_output_valid=qwen.raw_output_valid,
        qwen_failure_code=qwen.failure_code,
        deterministic_intent=deterministic_intent,
        deterministic_status=deterministic.status.value,
        deterministic_filters=_json_value(deterministic_filters),
        validation_outcome=qwen.validation.status.value,
        field_differences=differences,
        deterministic_field_differences=deterministic_differences,
        clarification_correct=clarification_correct,
        plan_supported=plan_supported,
    )


def evaluate_corpus(
    cases: Iterable[ParserCase] = CASES,
    qwen_interpreter=None,
    *,
    limit: int | None = None,
    check_plan: bool = True,
) -> EvaluationRun:
    """Evaluate a controlled number of corpus cases.

    ``limit`` avoids sending the complete 295-case corpus through the local
    model by accident.  Pass ``None`` only when the caller deliberately wants
    the whole corpus.
    """

    qwen_interpreter = qwen_interpreter or QwenInterpreter()
    selected = list(cases)
    if limit is not None:
        selected = selected[:limit]
    records = tuple(
        evaluate_case(case, qwen_interpreter, check_plan=check_plan)
        for case in selected
    )
    return EvaluationRun(
        model_name=qwen_interpreter.model,
        prompt_version=PROMPT_VERSION,
        generation_options=dict(qwen_interpreter.generation_options),
        total_cases=len(records),
        records=records,
    )


def _specified(record: EvaluationRecord, field: str) -> bool:
    value = record.expected_filters.get(field)
    return value is not None and value != [] and value != ()


def _field_metric(records, field: str, actual_key: str = "qwen_filters") -> dict:
    selected = [record for record in records if _specified(record, field)]
    correct = sum(
        1
        for record in selected
        if getattr(record, actual_key).get(field) == record.expected_filters.get(field)
    )
    return {"correct": correct, "total": len(selected)}


def _pressure_depth_metric(records, actual_key: str = "qwen_filters") -> dict:
    selected = [
        record for record in records
        if _specified(record, "pressure") or _specified(record, "depth")
    ]
    correct = sum(
        1
        for record in selected
        if getattr(record, actual_key).get("pressure")
        == record.expected_filters.get("pressure")
        and getattr(record, actual_key).get("depth")
        == record.expected_filters.get("depth")
    )
    return {"correct": correct, "total": len(selected)}


def _interpreter_summary(records, *, prefix: str) -> dict:
    def actual(record):
        return getattr(record, f"{prefix}_filters")

    fields = (
        "parameters",
        "wmos",
        "cycles",
        "values",
        "data_mode",
        "aggregations",
        "qc_scope",
        "qc_flags",
        "pressure",
        "depth",
        "limit",
        "sort_field",
        "sort_direction",
        "has_time",
        "has_bbox",
    )
    result = {field: _field_metric(records, field, f"{prefix}_filters") for field in fields}
    result["pressure_depth_distinction"] = _pressure_depth_metric(
        records, f"{prefix}_filters"
    )
    total = len(records)
    result["intent_accuracy"] = (
        sum(
            getattr(record, f"{prefix}_intent") == record.expected_intent
            for record in records
        ) / total if total else 0.0
    )
    result["status_accuracy"] = (
        sum(
            getattr(record, f"{prefix}_status") == record.expected_status
            for record in records
        ) / total if total else 0.0
    )
    differences_attr = (
        "field_differences"
        if prefix == "qwen"
        else "deterministic_field_differences"
    )
    result["overall_query_spec_correct"] = sum(
        1
        for record in records
        if getattr(record, f"{prefix}_intent") == record.expected_intent
        and getattr(record, f"{prefix}_status") == record.expected_status
        and not getattr(record, differences_attr)
    )
    result["clarification_correct"] = sum(
        1
        for record in records
        if record.expected_status == ValidationStatus.NEEDS_CLARIFICATION.value
        and getattr(record, f"{prefix}_status")
        == ValidationStatus.NEEDS_CLARIFICATION.value
    )
    result["clarification_total"] = sum(
        1
        for record in records
        if record.expected_status == ValidationStatus.NEEDS_CLARIFICATION.value
    )
    if prefix == "qwen":
        result["unsupported_recognition"] = {
            "correct": sum(
                1
                for record in records
                if record.expected_status == ValidationStatus.UNSUPPORTED.value
                and record.qwen_status == ValidationStatus.UNSUPPORTED.value
            ),
            "total": sum(
                1
                for record in records
                if record.expected_status == ValidationStatus.UNSUPPORTED.value
            ),
        }
        result["plan_supported"] = sum(
            1 for record in records if record.plan_supported is True
        )
        result["valid_without_plan"] = sum(
            1
            for record in records
            if record.qwen_status == ValidationStatus.VALID.value
            and record.plan_supported is False
        )
    return result


def summarise(records: tuple[EvaluationRecord, ...]) -> dict:
    """Return per-field metrics for both interpreters, not one blended score."""

    return {
        "total": len(records),
        "qwen": _interpreter_summary(records, prefix="qwen"),
        "deterministic": _interpreter_summary(records, prefix="deterministic"),
    }


def run_to_dict(run: EvaluationRun) -> dict:
    return {
        "model_name": run.model_name,
        "prompt_version": run.prompt_version,
        "generation_options": run.generation_options,
        "total_cases": run.total_cases,
        "summary": summarise(run.records),
        "records": [asdict(record) for record in run.records],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    interpreter = QwenInterpreter()
    if not interpreter.is_available():
        result = {
            "error": "ollama_or_qwen3_unavailable",
            "model_name": interpreter.model,
            "prompt_version": PROMPT_VERSION,
            "message": (
                "Ollama/Qwen3 is not available; no model evaluation was run. "
                "Install/start Ollama and pull the model, then rerun."
            ),
        }
        print(json.dumps(result, indent=2))
        return 2

    limit = None if args.all else max(0, args.limit)
    run = evaluate_corpus(limit=limit, check_plan=True)
    payload = run_to_dict(run)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"model={payload['model_name']} prompt={payload['prompt_version']} "
            f"cases={payload['total_cases']}"
        )
        print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual evaluation entry point
    raise SystemExit(main())
