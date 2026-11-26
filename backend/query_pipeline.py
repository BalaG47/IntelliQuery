"""Isolated end-to-end deterministic query pipeline (Phase 3C-E).

Question -> query_parser -> QuerySpec -> validation -> query_planner ->
QueryPlan -> controlled execution -> structured result.

This module is not connected to the production chat route.  It accepts an
existing SQLAlchemy engine, so tests can use a fake engine or a read-only live
database without starting Flask, ChromaDB or Ollama.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from bgc_calculations import (
    QC_POLICY_ALL_FINITE,
    QC_POLICY_GOOD_ONLY,
    calculate_bgc_statistics,
    filter_rows_by_bgc_threshold,
)
from query_parser import ParsedQuery, parse_query
from query_planner import QueryPlan, execute_spec, plan_query
from query_spec import DEFAULT_LIST_LIMIT, MAX_RESULT_LIMIT, ValidationStatus


class PipelineStatus(str, Enum):
    EXECUTED = "executed"
    NO_DATABASE_QUERY = "no_database_query"
    INVALID = "invalid"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"
    EXECUTION_ERROR = "execution_error"


@dataclass(frozen=True)
class PipelineResult:
    """A staged, inspectable outcome; never a disguised empty success."""

    question: str
    status: PipelineStatus
    parsed: ParsedQuery
    plan: QueryPlan | None = None
    rows: tuple[dict, ...] = ()
    result_shape: str | None = None
    message: str | None = None
    error: str | None = None


# Deliberately narrow: these are the Phase 3C-C dangling-pressure forms whose
# pressure filter is known to be absent from the resulting QuerySpec.  The
# parser is not modified; the pipeline refuses to execute an incomplete
# interpretation instead of silently dropping the pressure request.
_INCOMPLETE_PRESSURE = re.compile(
    r"\bpressure\s+(?:between|from)\s+-?\d+(?:\.\d+)?\s*"
    r"(?:dbar|decibar|db)?\b(?!\s*(?:-|to|and)\s*-?\d)",
    re.IGNORECASE,
)


def _has_incomplete_pressure_phrase(question: str, pressure_range) -> bool:
    return pressure_range is None and bool(_INCOMPLETE_PRESSURE.search(question))


def _result(
    *,
    question: str,
    status: PipelineStatus,
    parsed: ParsedQuery,
    plan: QueryPlan | None = None,
    rows: tuple[dict, ...] = (),
    result_shape: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> PipelineResult:
    return PipelineResult(
        question=question,
        status=status,
        parsed=parsed,
        plan=plan,
        rows=rows,
        result_shape=result_shape,
        message=message,
        error=error,
    )


def run_pipeline(question: str, engine=None, capabilities=None) -> PipelineResult:
    """Run the deterministic pipeline for one question.

    No SQL is executed for parser failures, validation failures, unsupported
    plans or no-database intents.  A missing engine for a SQL plan is a
    structured execution error, not a successful empty result.
    """

    parsed = parse_query(question)
    status = parsed.status

    if status is ValidationStatus.INVALID:
        return _result(
            question=question,
            status=PipelineStatus.INVALID,
            parsed=parsed,
            message=parsed.reason or "The question could not be parsed.",
        )
    if status is ValidationStatus.NEEDS_CLARIFICATION:
        return _result(
            question=question,
            status=PipelineStatus.NEEDS_CLARIFICATION,
            parsed=parsed,
            message=parsed.reason or "The question needs clarification.",
        )
    if status is ValidationStatus.UNSUPPORTED:
        return _result(
            question=question,
            status=PipelineStatus.UNSUPPORTED,
            parsed=parsed,
            message=parsed.reason or "The question is not supported.",
        )

    spec = parsed.validation.spec
    if status is not ValidationStatus.VALID or spec is None:
        return _result(
            question=question,
            status=PipelineStatus.INVALID,
            parsed=parsed,
            message=parsed.reason or "The validation result has no QuerySpec.",
        )

    if _has_incomplete_pressure_phrase(parsed.text, spec.pressure_range):
        return _result(
            question=question,
            status=PipelineStatus.UNSUPPORTED,
            parsed=parsed,
            message=(
                "The pressure expression is incomplete and was not interpreted "
                "as a pressure filter. Supply a complete range in dbar, for "
                "example 'pressure between 0 and 500 dbar'."
            ),
        )

    plan = plan_query(spec, capabilities)

    if not plan.supported:
        return _result(
            question=question,
            status=PipelineStatus.UNSUPPORTED,
            parsed=parsed,
            plan=plan,
            message=plan.reason or "The query plan is not supported.",
        )

    if plan.sql is None:
        return _result(
            question=question,
            status=PipelineStatus.NO_DATABASE_QUERY,
            parsed=parsed,
            plan=plan,
            result_shape=plan.result_shape.value,
        )

    if engine is None:
        return _result(
            question=question,
            status=PipelineStatus.EXECUTION_ERROR,
            parsed=parsed,
            plan=plan,
            result_shape=plan.result_shape.value,
            error="No database engine was supplied; no query was executed.",
        )

    try:
        executed = execute_spec(spec, engine, capabilities)
    except Exception as exc:
        # Log only the exception type: SQL, bound values, connection details
        # and credentials must not be written to the application log.
        logging.error(
            "Deterministic query execution failed (%s)",
            type(exc).__name__,
        )
        return _result(
            question=question,
            status=PipelineStatus.EXECUTION_ERROR,
            parsed=parsed,
            plan=plan,
            result_shape=plan.result_shape.value,
            error="Query execution failed; no result was produced.",
        )

    rows = list(executed["rows"])
    if plan.post_processing:
        qc_policy = (
            QC_POLICY_GOOD_ONLY
            if spec.qc_filter is not None
            else QC_POLICY_ALL_FINITE
        )
        values_mode = spec.values.value
        if "bgc_numeric_filter" in plan.post_processing and spec.numeric_filter is not None:
            nf = spec.numeric_filter
            rows = filter_rows_by_bgc_threshold(
                rows,
                nf.parameter,
                nf.operator.value,
                nf.value,
                nf.upper_value,
                scope=nf.scope,
                aggregation=nf.aggregation.value if nf.aggregation else None,
                values_mode=values_mode,
                qc_policy=qc_policy,
            )
            limit = spec.limit if spec.limit is not None else (
                MAX_RESULT_LIMIT if nf.scope == "float" else DEFAULT_LIST_LIMIT
            )
            rows = rows[:limit]
        elif "bgc_compare" in plan.post_processing:
            # Per-profile (or per-WMO if multiple WMOs) comparison results.
            limit = spec.limit if spec.limit is not None else DEFAULT_LIST_LIMIT
            comp_rows = []
            for r in rows[:limit]:
                for param in spec.parameters:
                    calc = calculate_bgc_statistics(
                        [r],
                        param,
                        scope="profile",
                        aggregation_type="COMPARE",
                        values_mode=values_mode,
                        qc_policy=qc_policy,
                    )
                    comp_rows.append(calc.to_dict())
            rows = comp_rows
        elif "bgc_calculate" in plan.post_processing:
            agg_type = (
                ",".join(a.value for a in spec.aggregations)
                if spec.aggregations
                else "SUMMARY"
            )
            scope = (
                "float"
                if len(spec.wmo_numbers) == 1
                else ("profile" if len(rows) == 1 else "result_set")
            )
            calc_rows = []
            for param in spec.parameters:
                calc = calculate_bgc_statistics(
                    rows,
                    param,
                    scope=scope,
                    aggregation_type=agg_type,
                    values_mode=values_mode,
                    qc_policy=qc_policy,
                )
                calc_rows.append(calc.to_dict())
            rows = calc_rows

    return _result(
        question=question,
        status=PipelineStatus.EXECUTED,
        parsed=parsed,
        plan=plan,
        rows=tuple(rows),
        result_shape=plan.result_shape.value,
    )
