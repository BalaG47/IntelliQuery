"""Shadow-mode evaluation of the deterministic parser (Phase 3C-C).

The shadow harness compares the **existing** production router with the new
:mod:`query_parser` for the same question and records both decisions.  It is a
pure read-only observer:

* it never calls ``/get-question`` and never changes its response;
* it never executes SQL or touches a database;
* it never raises into a caller - a parser failure is recorded as data;
* it is not imported by any production module.

Typical use::

    python -m query_shadow "Compare WMO 1900121 and 2900226"

or from code::

    from query_shadow import evaluate_query, summarise
    record = evaluate_query("Show temperature for WMO 1900121")
    summary = summarise([record])
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass

from query_parser import parse_query
from query_router import ArgoQueryRouter, Route

__all__ = [
    "ShadowRecord",
    "evaluate_query",
    "evaluate_queries",
    "summarise",
    "format_report",
]

#: Router routes that represent a structured (exact) data request.
_ROUTER_STRUCTURED_INTENTS = frozenset(
    {
        "profile_detail",
        "list_profiles",
        "compare",
        "aggregate",
        "parameter_availability",
        "metadata_info",
    }
)

_ROUTE_EXPECTED_INTENT = {
    Route.GENERAL_CONVERSATION.value: "conversational",
    Route.CHROMA_SEMANTIC.value: "semantic_discovery",
    Route.MYSQL_FLOAT_ENUMERATION.value: "enumerate_floats",
    Route.MYSQL_PROFILE_COUNTS.value: "count_profiles",
    Route.MYSQL_WMO_PROFILE_COUNT.value: "count_profiles",
    Route.MYSQL_REGIONAL_TREND.value: "aggregate",
}


@dataclass(frozen=True)
class ShadowRecord:
    """One side-by-side comparison.  Contains no user identifiers."""

    query: str
    router_route: str
    router_wmo: int | None
    parser_intent: str | None
    parser_status: str
    parameters: tuple[str, ...]
    filters: tuple[str, ...]
    clarification_reason: str | None
    agree: bool
    parser_error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _parser_filters(spec) -> tuple[str, ...]:
    if spec is None:
        return ()

    filters: list[str] = []
    if spec.wmo_numbers:
        filters.append("wmo=" + ",".join(str(value) for value in spec.wmo_numbers))
    if spec.cycle_numbers:
        filters.append("cycle=" + ",".join(str(value) for value in spec.cycle_numbers))
    if spec.time_range is not None:
        filters.append("time_range")
    if spec.bounding_box is not None:
        filters.append("bounding_box")
    if spec.pressure_range is not None:
        filters.append(f"pressure={spec.pressure_range[0]}-{spec.pressure_range[1]}")
    if spec.depth_requested or spec.depth_range_m is not None:
        filters.append("depth")
    if spec.radius_search is not None:
        filters.append("radius")
    if spec.qc_filter is not None:
        filters.append(
            f"qc:{spec.qc_filter.scope.value}="
            + ",".join(sorted(spec.qc_filter.flags))
        )
    if spec.data_mode is not None:
        filters.append(f"data_mode={spec.data_mode.value}")
    if spec.values.value != "raw":
        filters.append(f"values={spec.values.value}")
    if spec.aggregations:
        filters.append(
            "aggregations=" + ",".join(item.value for item in spec.aggregations)
        )
    if spec.limit is not None:
        filters.append(f"limit={spec.limit}")
    if spec.sort.direction.value != "desc" or spec.sort.field.value != "profile_time":
        filters.append(
            f"sort={spec.sort.field.value}:{spec.sort.direction.value}"
        )
    return tuple(filters)


def _intents_agree(route: str, intent: str | None) -> bool:
    if intent is None:
        return False
    if route == Route.MYSQL_WMO.value:
        return intent in _ROUTER_STRUCTURED_INTENTS
    expected = _ROUTE_EXPECTED_INTENT.get(route)
    if expected is None:
        return False
    return intent == expected


def evaluate_query(query: str) -> ShadowRecord:
    """Compare the production route with the new parser for one query."""

    normalized = " ".join((query or "").lower().split())
    route = ArgoQueryRouter.route(normalized)

    error: str | None = None
    try:
        parsed = parse_query(query)
        intent = parsed.intent.value if parsed.intent is not None else None
        status = parsed.status.value
        parameters = (
            tuple(item.value for item in parsed.spec.parameters)
            if parsed.spec is not None
            else ()
        )
        filters = _parser_filters(parsed.spec)
        reason = parsed.reason
    except Exception as exc:  # defensive: shadow failures are data, not crashes
        intent = None
        status = "error"
        parameters = ()
        filters = ()
        reason = None
        error = f"{type(exc).__name__}: {exc}"

    return ShadowRecord(
        query=query,
        router_route=route.route.value,
        router_wmo=route.wmo_number,
        parser_intent=intent,
        parser_status=status,
        parameters=parameters,
        filters=filters,
        clarification_reason=reason,
        agree=_intents_agree(route.route.value, intent),
        parser_error=error,
    )


def evaluate_queries(queries) -> list[ShadowRecord]:
    return [evaluate_query(query) for query in queries]


def summarise(records) -> dict:
    """Aggregate counts used by the evaluation report."""

    total = len(records)
    agreements = sum(1 for record in records if record.agree)
    errors = sum(1 for record in records if record.parser_error)
    clarifications = sum(
        1 for record in records if record.parser_status == "needs_clarification"
    )
    unsupported = sum(1 for record in records if record.parser_status == "unsupported")
    invalid = sum(1 for record in records if record.parser_status == "invalid")

    return {
        "total": total,
        "agreements": agreements,
        "disagreements": total - agreements,
        "agreement_rate": round(agreements / total, 4) if total else 0.0,
        "parser_errors": errors,
        "clarifications": clarifications,
        "unsupported": unsupported,
        "invalid": invalid,
    }


def format_report(records) -> str:
    """Return a readable table plus a JSON summary (no private data)."""

    lines = [
        f"{'query':52} {'router':24} {'parser':22} {'status':20} agree",
        "-" * 130,
    ]
    for record in records:
        lines.append(
            f"{record.query[:52]:52} {record.router_route:24} "
            f"{str(record.parser_intent):22} {record.parser_status:20} "
            f"{'yes' if record.agree else 'no'}"
        )
        if record.parser_error:
            lines.append(f"    parser error: {record.parser_error}")

    lines.append("")
    lines.append(json.dumps(summarise(records), indent=2))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual, opt-in entry point
    from query_parser_test_cases import CASES

    queries = sys.argv[1:] or [case.query for case in CASES]
    print(format_report(evaluate_queries(queries)))
