"""Common query-interpreter contract and strict Qwen3 adapter (Phase 4A).

Both the deterministic parser and Qwen3 produce an ``Interpretation``.  A model
candidate is only a candidate: it reaches the authoritative status through
``query_spec.validate_spec``, and this module never plans or executes SQL.

The Qwen adapter accepts only a constrained JSON object.  Unknown fields,
malformed JSON, SQL/database fields, invalid enum values and wrong types are
rejected rather than partially accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from query_parser import parse_query
from query_spec import (
    Aggregation,
    BoundingBox,
    DataMode,
    GroupBy,
    Intent,
    MeasurementValues,
    Parameter,
    QcFilter,
    QcScope,
    QuerySpec,
    SortDirection,
    SortField,
    SortSpec,
    TimeRange,
    ValidationResult,
    ValidationStatus,
    validate_spec,
)


DEFAULT_MODEL = "qwen3:8b"
PROMPT_VERSION = "phase4a-v3.3"
GENERATION_OPTIONS = {"temperature": 0.1}


class InterpreterFailure(str, Enum):
    OLLAMA_UNAVAILABLE = "ollama_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    FORBIDDEN_SQL = "forbidden_sql"
    VALIDATION_FAILED = "validation_failed"
    OLLAMA_ERROR = "ollama_error"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class InterpreterTelemetry:
    """Diagnostic evidence for one interpreter call.

    The raw response is untrusted text retained for audit only. It is never
    executed and must not be interpreted as instructions.
    """

    raw_response: str | None = None
    json_parse_ok: bool | None = None
    json_error: str | None = None
    decoder_accepted: bool | None = None
    decoder_error_code: str | None = None
    decoder_error_message: str | None = None
    rejected_field: str | None = None
    expected_type: str | None = None


@dataclass(frozen=True)
class Interpretation:
    """One interpreter result.

    ``status`` is derived from ``validation``; there is deliberately no settable
    status field, so a caller cannot mark a candidate VALID by assignment.
    """

    interpreter: str
    validation: ValidationResult
    candidate_spec: QuerySpec | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    fallback_used: bool = False
    fallback_reason: str | None = None
    model_name: str | None = None
    raw_output_valid: bool | None = None
    failure_code: str | None = None
    original_interpreter: str | None = None
    original_status: ValidationStatus | None = None
    telemetry: InterpreterTelemetry | None = None

    @property
    def status(self) -> ValidationStatus:
        return self.validation.status


class QwenOutputError(ValueError):
    """A strict-decoder rejection with a stable code and optional field metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        expected_type: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.expected_type = expected_type


@dataclass(frozen=True)
class DecodedQwenOutput:
    spec: QuerySpec | None
    clarification: str | None
    unsupported_reason: str | None


_ALLOWED_KEYS = frozenset(
    {
        "intent",
        "parameters",
        "wmo_numbers",
        "cycle_numbers",
        "time_range",
        "bounding_box",
        "pressure_range",
        "depth_range_m",
        "depth_requested",
        "values",
        "qc_filter",
        "data_mode",
        "aggregations",
        "group_by",
        "sort",
        "limit",
        "semantic_query",
        "notes",
        "clarification",
        "unsupported_reason",
    }
)

_FORBIDDEN_KEYS = frozenset(
    {
        "sql",
        "query",
        "statement",
        "table",
        "tables",
        "column",
        "columns",
        "connection",
        "database",
        "tool",
        "tool_call",
        "tool_calls",
        "function",
        "code",
        "execute",
        "execution",
        "script",
    }
)

_QUERY_FIELDS = _ALLOWED_KEYS - {
    "intent",
    "clarification",
    "unsupported_reason",
}


def _fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    expected_type: str | None = None,
) -> QwenOutputError:
    return QwenOutputError(
        code,
        message,
        field=field,
        expected_type=expected_type,
    )


def _string(value, field: str) -> str:
    if not isinstance(value, str):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be a string.",
            field=field,
            expected_type="string",
        )
    return value


def _nonempty_string(value, field: str) -> str:
    text = _string(value, field)
    if not text.strip():
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must not be empty.",
            field=field,
            expected_type="non-empty string",
        )
    return text


def _int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be an integer.",
            field=field,
            expected_type="integer",
        )
    return value


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be a number.",
            field=field,
            expected_type="number",
        )
    return float(value)


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be a boolean.",
            field=field,
            expected_type="boolean",
        )
    return value


def _list(value, field: str) -> list:
    if not isinstance(value, list):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be a list.",
            field=field,
            expected_type="list",
        )
    return value


def _pair(value, field: str) -> tuple[float, float]:
    items = _list(value, field)
    if len(items) != 2:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must have two values.",
            field=field,
            expected_type="array of two numbers",
        )
    return (_number(items[0], f"{field}[0]"), _number(items[1], f"{field}[1]"))


def _enum(value, enum_type, field: str):
    if not isinstance(value, str):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be a string.",
            field=field,
            expected_type="string enum",
        )
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} has invalid value {value!r}; expected one of: {allowed}.",
            field=field,
            expected_type=f"one of: {allowed}",
        )


def _datetime(value, field: str) -> datetime:
    text = _string(value, field)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be an ISO-8601 date or datetime.",
            field=field,
            expected_type="ISO-8601 string",
        )


def _object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be an object.",
            field=field,
            expected_type="object",
        )
    return value


def _check_keys(value: dict, allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        rejected = sorted(unknown)[0]
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} has unexpected field(s): {', '.join(sorted(unknown))}.",
            field=f"{field}.{rejected}",
            expected_type="not allowed",
        )


def decode_qwen_output(payload: dict) -> DecodedQwenOutput:
    """Strictly convert one Qwen JSON object into a candidate or outcome.

    The function never validates a QuerySpec; the caller must do that.  It only
    ensures the output has the approved shape and types.
    """

    if not isinstance(payload, dict):
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            "Output must be a JSON object.",
            field="response",
            expected_type="object",
        )

    for key in payload:
        if not isinstance(key, str):
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                "Field names must be strings.",
                field="<non-string key>",
                expected_type="string field name",
            )
        normalised = key.lower()
        if normalised in _FORBIDDEN_KEYS:
            raise _fail(
                InterpreterFailure.FORBIDDEN_SQL.value,
                f"Model output contains forbidden field {key!r}.",
                field=key,
                expected_type="forbidden field",
            )
        if key not in _ALLOWED_KEYS:
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                f"Unexpected field {key!r}.",
                field=key,
                expected_type="not allowed",
            )

    if "intent" not in payload:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            "Missing required field 'intent'.",
            field="intent",
            expected_type="string enum",
        )

    intent = _enum(payload["intent"], Intent, "intent")

    has_clarification = "clarification" in payload
    has_unsupported = "unsupported_reason" in payload

    if has_clarification and has_unsupported:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            "Output cannot contain both clarification and unsupported_reason.",
            field="clarification/unsupported_reason",
            expected_type="mutually exclusive fields",
        )

    if has_clarification or has_unsupported:
        query_fields = sorted(key for key in _QUERY_FIELDS if key in payload)
        if query_fields:
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                "A clarification or unsupported result cannot carry query fields.",
                field=query_fields[0],
                expected_type="must be omitted for clarification or unsupported output",
            )
        if has_clarification:
            return DecodedQwenOutput(
                spec=None,
                clarification=_nonempty_string(payload["clarification"], "clarification"),
                unsupported_reason=None,
            )
        return DecodedQwenOutput(
            spec=None,
            clarification=None,
            unsupported_reason=_nonempty_string(
                payload["unsupported_reason"], "unsupported_reason"
            ),
        )

    if intent is Intent.CLARIFY:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            "intent 'clarify' requires a non-empty clarification field.",
            field="clarification",
            expected_type="non-empty string",
        )

    parameters = ()
    if "parameters" in payload:
        parameters = tuple(
            _enum(item, Parameter, "parameters[]")
            for item in _list(payload["parameters"], "parameters")
        )

    wmos = ()
    if "wmo_numbers" in payload:
        wmos = tuple(
            _int(item, "wmo_numbers[]")
            for item in _list(payload["wmo_numbers"], "wmo_numbers")
        )

    cycles = ()
    if "cycle_numbers" in payload:
        cycles = tuple(
            _int(item, "cycle_numbers[]")
            for item in _list(payload["cycle_numbers"], "cycle_numbers")
        )

    time_range = None
    if "time_range" in payload:
        obj = _object(payload["time_range"], "time_range")
        _check_keys(obj, {"start", "end"}, "time_range")
        time_range = TimeRange(
            start=_datetime(obj["start"], "time_range.start") if "start" in obj else None,
            end=_datetime(obj["end"], "time_range.end") if "end" in obj else None,
        )

    bounding_box = None
    if "bounding_box" in payload:
        obj = _object(payload["bounding_box"], "bounding_box")
        allowed = {"min_latitude", "max_latitude", "min_longitude", "max_longitude"}
        _check_keys(obj, allowed, "bounding_box")
        bounding_box = BoundingBox(
            **{
                key: _number(obj[key], f"bounding_box.{key}")
                for key in allowed
                if key in obj
            }
        )

    pressure_range = None
    if "pressure_range" in payload:
        pressure_range = _pair(payload["pressure_range"], "pressure_range")

    depth_range_m = None
    if "depth_range_m" in payload:
        depth_range_m = _pair(payload["depth_range_m"], "depth_range_m")

    depth_requested = False
    if "depth_requested" in payload:
        depth_requested = _bool(payload["depth_requested"], "depth_requested")

    values = MeasurementValues.RAW
    if "values" in payload:
        values = _enum(payload["values"], MeasurementValues, "values")

    qc_filter = None
    if "qc_filter" in payload:
        obj = _object(payload["qc_filter"], "qc_filter")
        _check_keys(obj, {"scope", "flags"}, "qc_filter")
        if "scope" not in obj or "flags" not in obj:
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                "qc_filter requires scope and flags.",
                field="qc_filter",
                expected_type="object with scope and flags",
            )
        qc_filter = QcFilter(
            scope=_enum(obj["scope"], QcScope, "qc_filter.scope"),
            flags=frozenset(
                _string(item, "qc_filter.flags[]")
                for item in _list(obj["flags"], "qc_filter.flags")
            ),
        )

    data_mode = None
    if "data_mode" in payload:
        data_mode = _enum(payload["data_mode"], DataMode, "data_mode")

    aggregations = ()
    if "aggregations" in payload:
        aggregations = tuple(
            _enum(item, Aggregation, "aggregations[]")
            for item in _list(payload["aggregations"], "aggregations")
        )

    group_by = None
    if "group_by" in payload:
        group_by = _enum(payload["group_by"], GroupBy, "group_by")

    sort = SortSpec()
    if "sort" in payload:
        obj = _object(payload["sort"], "sort")
        _check_keys(obj, {"field", "direction"}, "sort")
        field = _enum(obj.get("field", "profile_time"), SortField, "sort.field")
        direction = _enum(
            obj.get("direction", "desc"), SortDirection, "sort.direction"
        )
        sort = SortSpec(field=field, direction=direction)

    limit = None
    if "limit" in payload:
        if payload["limit"] is not None:
            limit = _int(payload["limit"], "limit")

    semantic_query = None
    if "semantic_query" in payload:
        if payload["semantic_query"] is not None:
            semantic_query = _string(payload["semantic_query"], "semantic_query")

    notes = ()
    if "notes" in payload:
        notes = tuple(
            _string(item, "notes[]")
            for item in _list(payload["notes"], "notes")
        )

    spec = QuerySpec(
        intent=intent,
        parameters=parameters,
        wmo_numbers=wmos,
        cycle_numbers=cycles,
        time_range=time_range,
        bounding_box=bounding_box,
        pressure_range=pressure_range,
        depth_range_m=depth_range_m,
        depth_requested=depth_requested,
        qc_filter=qc_filter,
        data_mode=data_mode,
        values=values,
        aggregations=aggregations,
        group_by=group_by,
        sort=sort,
        limit=limit,
        semantic_query=semantic_query,
        notes=notes,
    )
    return DecodedQwenOutput(spec=spec, clarification=None, unsupported_reason=None)


def interpret_deterministic(question: str) -> Interpretation:
    """Adapt the approved deterministic parser to the common contract."""

    parsed = parse_query(question)
    diagnostics = tuple(
        Diagnostic(code="parser", message=message) for message in parsed.diagnostics
    )
    return Interpretation(
        interpreter="deterministic",
        validation=parsed.validation,
        candidate_spec=parsed.spec,
        diagnostics=diagnostics,
        raw_output_valid=None,
    )


def _failure_interpretation(
    *,
    code: str,
    message: str,
    model_name: str | None = None,
    raw_output_valid: bool | None = False,
    telemetry: InterpreterTelemetry | None = None,
) -> Interpretation:
    return Interpretation(
        interpreter="qwen3",
        validation=ValidationResult(
            status=ValidationStatus.INVALID,
            reason=message,
        ),
        diagnostics=(Diagnostic(code=code, message=message),),
        model_name=model_name,
        raw_output_valid=raw_output_valid,
        failure_code=code,
        telemetry=telemetry,
    )


def _classify_ollama_exception(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in message or "timeout" in message:
        return InterpreterFailure.TIMEOUT.value
    if "not found" in message or "does not exist" in message or "pull" in message:
        return InterpreterFailure.MODEL_UNAVAILABLE.value
    if (
        "connection" in message
        or "connect" in message
        or "refused" in message
        or "unreachable" in message
    ):
        return InterpreterFailure.OLLAMA_UNAVAILABLE.value
    return InterpreterFailure.OLLAMA_ERROR.value


def _extract_raw_response(response) -> str:
    """Return the model response as untrusted text, without normalization."""

    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        value = response.get("response", "")
        return value if isinstance(value, str) else ""
    value = getattr(response, "response", "")
    return value if isinstance(value, str) else ""


def _extract_response_text(response) -> str:
    return _extract_raw_response(response).strip()


_PROMPT_TEMPLATE = (
    "You are a structured query interpreter for an ARGO oceanographic database. Return exactly one JSON object and no Markdown, prose or comments.\n\n"
    "You must not return SQL, table names, column names, connection details, code, tool calls or execution instructions. The downstream system validates and plans the query itself.\n\n"
    "Allowed top-level fields and their exact JSON shapes:\n  intent one of: enumerate_floats, count_profiles, list_profiles, profile_detail, aggregate, compare, parameter_availability, metadata_info, conversational, semantic_discovery, clarify\n  parameters array of strings from: TEMP, PSAL, PRES, DOXY, CHLA, BBP700, NITRATE, PH_IN_SITU_TOTAL (never a bare string or nested array)\n  wmo_numbers array of JSON integers, e.g. [1900121]\n  cycle_numbers array of JSON integers (never quoted, never nested)\n  time_range {\"start\":\"ISO-8601\",\"end\":\"ISO-8601\"}\n  bounding_box {\"min_latitude\":..,\"max_latitude\":..,\"min_longitude\":..,\"max_longitude\":..}\n  pressure_range [min,max] in dbar, exactly two numbers\n  depth_range_m [min,max] in metres, exactly two numbers\n  depth_requested true or false, only when depth is mentioned\n  values \"raw\" or \"adjusted\" (measurement selection only)\n  data_mode \"R\" (real-time) or \"D\" (delayed); profile-level only\n  qc_filter {\"scope\":\"profile\" or \"measurement\",\"flags\":[\"A\",\"B\"]}; flags are always strings\n  aggregations array from: COUNT, MEAN, MIN, MAX\n  group_by \"year\", \"month\", \"wmo\" or \"profile\"; only with an aggregation\n  sort {\"field\":\"profile_time\"|\"cycle_number\"|\"wmo_number\"|\"latitude\"|\"longitude\"|\"profile_id\",\"direction\":\"asc\"|\"desc\"}\n  limit JSON integer\n  semantic_query search text string\n  notes array of strings\n  clarification non-empty string\n  unsupported_reason non-empty string\n\n"
    "Decide the operation first: list profiles, count profiles, enumerate floats, check parameter availability, retrieve one profile's detail, compare, aggregate, run a semantic search, or converse. Extract only the filters that operation needs.\n\n"
    "Clarify instead of guessing. Return {\"intent\":\"clarify\",\"clarification\":\"<one specific question>\"} with no other field when:\n- a number, cycle, WMO number or date cannot be attached to a field with confidence ('cycles 10 to 20 for temperature 25');\n- a range or list is incomplete, unbalanced or contradictory ('pressure between 0 and', 'cycles 10 to');\n- two cycle expressions compete, or a cycle count comes with explicit cycles;\n- the request needs a scientific choice the user has not made.\nDecide in this order for every filter. (1) Structure: is every required part supplied? A range needs both bounds, a bounding box needs four numbers. If a part is missing, clarify; never fill it with 0, a default, or a guessed bound. (2) Reading: do two expressions compete for the same field? Clarify. (3) Validity: the structure is complete and unambiguous but a value is impossible, reversed or out of range. Emit the filter unchanged and let validation reject it; never repair it.\nA complete range with both bounds and an obvious field ('cycles 10 to 20', 'temperature between 0 and 500 dbar') is not ambiguous: extract it. Never invent a filter. Do not add time_range, pressure_range, cycle_numbers, wmo_numbers, parameters, data_mode, limit or sort the question does not support. A clarification is always better than a guessed valid query.\n\n"
    "Omit every field the question does not justify:\n- omit parameters unless a parameter is named; never list all eight because the question asks for profiles;\n- omit cycle_numbers unless a cycle clause is present;\n- omit bounding_box unless both latitude and longitude are given;\n- omit limit and sort unless the question asks for a count or a profile order ('the latest three profiles' is a count; 'the latest five cycles' is not);\n- omit data_mode, depth and time_range unless stated.\n\n"
    "Cycle expressions. One cycle list ('cycles 10 and 11') or one range ('cycles 10 to 20', 'cycles 10-20') is complete: expand it to every integer, and keep it beside a parameter, WMO, year, sort, QC or pressure filter. Commas, semicolons, 'and', '&' and one balanced pair of parentheses are separators, not ambiguity, when they join values of the same form: 'cycles 10; 11 and 12' is [10,11,12] and 'cycles (10 and 11)' is [10,11]; never clarify a punctuation style. Clarify competing forms: a range plus extra values ('cycles 10 to 20 and 25'), a list plus a range ('cycles 10 and 11, 12 to 15', 'cycles 10; 11 to 15'), and doubled or unbalanced parentheses ('cycles ((10 and 11))', 'cycles 10 and 11)'); never strip or balance parentheses. A sort is independent, not a conflict: 'sorted by highest cycle' and 'cycles 10 to 20 sorted by highest cycle' set sort cycle_number desc and are valid. Clarify a count or ranking of cycles that asks for a number ('the latest five cycles'): limit counts profiles, not cycles.\n\n"
    "Pressure. Emit pressure_range only when the question states dbar (or db); never infer the unit. It needs both bounds from one expression. A bare number is clarified: 'pressure 500' -> clarify, 'pressure 0 to 500' -> clarify, 'pressure 0-500' -> clarify, 'pressure between 0 and 500' -> clarify. With a unit: 'pressure 0 to 500 dbar' -> [0,500]; 'at 500 dbar' -> [500,500]. Clarify a missing bound ('pressure from WMO 1900121') or two competing expressions ('pressure 500 dbar and pressure 700 dbar'). A repeated identical clause ('pressure 500 dbar and pressure 500 dbar') is one filter [500,500], not a conflict. A reversed, negative or implausible range with units ('pressure 500 to 0 dbar', '-10 dbar') is emitted unchanged; validation rejects it. Never supply a default unit, bound or value.\n\n"
    "Geography. A bounding_box needs a latitude range and a longitude range: four numbers, a minimum and a maximum for each. Clarify a point ('near 15N, 65E', 'at latitude 15 longitude 75'), a single axis ('between latitude 10 and 20', 'at longitude 75'), or a missing bound ('latitude 10 and 20 longitude 70'); never fill a missing axis with 0 or a default, and never copy a bound from an example. A named region ('Arabian Sea', 'Indian Ocean') is ambiguous: clarify; a place name is a geographic filter, not a description, and never goes in semantic_query. A complete box with an out-of-range number ('longitude 170 and 185') is emitted unchanged; validation rejects it. Repeated identical clauses are one box; a complete box plus an extra single latitude or longitude ('... and longitude 75') is incomplete and clarified. Only an explicit distance ('within 100 km of 15N, 65E') is unsupported.\n\n"
    "Dates. A year introduced by 'in', 'during', 'year', 'from', 'since', 'until', 'after' or 'before' is a time_range for that year ('in 2025', 'until 2010', 'from 2020 to 2022'); an ISO-8601 date is a range for that day. An ambiguous numeric date ('01/02/2023') is clarified; 'Show profiles from 2024' -> time_range for 2024, and 'Show cycles 10 to 20 in 2025' is valid (cycles plus time_range). A year joined only by 'and', or trailing with no date word ('... for WMO 1900121 2025'), is ambiguous between a year and a cycle: clarify. Two competing years ('in 2020 and 2025') are ambiguous: clarify. Never fabricate a range around a date the user did not give. An impossible date ('2024-13-40') is never unsupported: emit the stated time_range.\n\n"
    "Invalid is not ambiguous. When the only defect is an out-of-range or contradictory value, return the normal filter JSON as if it were acceptable; the validator rejects it. Do not add clarification, unsupported_reason or notes, and do not repair the value. This covers reversed bounds ('pressure 500 to 0 dbar'), negative or implausible pressure ('-10 dbar'), latitude outside -90 to 90, longitude outside -180 to 180, a zero or malformed WMO ('WMO 0000000'), adjusted values with no parameter ('Show adjusted data'), measurement QC flags with no parameter, and mixed letter/digit QC ('QC flags 1, 2, and A'). Concretely: 'Show WMO 0000000' -> profile_detail with {\"wmo_numbers\":[0]}; 'Show adjusted data' -> {\"intent\":\"list_profiles\",\"values\":\"adjusted\"} with no parameters and never data_mode; 'Show profiles on 2024-13-40' -> {\"intent\":\"list_profiles\",\"time_range\":{\"start\":\"2024-13-40\",\"end\":\"2024-13-40\"}}. A missing latitude or longitude is ambiguous and clarified.\n\n"
    "Ranges. Expand an inclusive integer range: 'cycles 10 to 20' -> [10,11,12,13,14,15,16,17,18,19,20], never only the endpoints or a nested array.\n\n"
    "Measurement selection versus mode. 'raw'/'adjusted' set values; 'real-time'/'RT' and 'delayed-mode'/'DM' set data_mode. They are not interchangeable; 'adjusted' is not data_mode 'D', and data_mode is never applied to a single parameter.\n\n"
    "QC. Emit one qc_filter object. Scope is 'profile' for profile QC or overall quality, 'measurement' for flags attached to a parameter. Flags are strings; profile scope uses letters, measurement scope digits. 'good quality' means [\"A\",\"B\"] for profile scope and [\"1\",\"2\"] for measurement scope only when the question names profiles or measurements: 'Show good-quality profiles' -> profile [\"A\",\"B\"], 'Show good-quality temperature measurements' -> measurement [\"1\",\"2\"]. A bare 'good-quality data' leaves the scope open, so clarify. QC is a filter, not an intent. Profile scope accepts letter categories A, B, C and D: emit every flag listed ('profile QC flags A, B and C' is [\"A\",\"B\",\"C\"]), and emit one mixed list for validation to reject rather than choosing an alphabet.\n\n"
    "Depth versus pressure. Pressure is dbar in pressure_range. Depth in metres is depth_range_m or depth_requested=true, never converted to pressure; use depth_requested=true for a single depth.\n\n"
    "Sorting and counts. 'latest', 'newest', 'recent', 'most recent', 'highest' -> desc; 'first', 'oldest', 'earliest', 'lowest' -> asc. 'the latest three profiles' -> sort profile_time desc, limit 3; 'the first 3 profiles' -> asc, limit 3. Never turn a count into a cycle, WMO or pressure filter.\n\n"
    "Intent. 'How many ...' -> count_profiles. parameter_availability only when the question asks whether a parameter exists or which floats/profiles contain it; a plain 'Show <parameter>' is list_profiles. 'List all floats' or 'how many floats' -> enumerate_floats. 'Compare ...' -> compare with two WMOs or two parameters. 'Show/list/display profiles ...' -> list_profiles. Descriptive searches -> semantic_discovery; a qualitative description with no numeric filter ('cold deep water') is semantic_discovery with semantic_query. Greetings -> conversational. aggregate only with COUNT, MEAN, MIN, MAX; median, mode and percentile are unsupported.\n\n"
    "One identifier means one profile: exactly one WMO, float, platform or cycle plus a parameter or cycle range -> profile_detail ('Show me WMO 2900226'). With a date or year, use list_profiles. The bare token 'ID' is not a platform cue; only 'float', 'WMO', 'platform' or 'cycle' attaches an identifier, so 'Show float 1900121' -> wmo_numbers [1900121], while 'Show profiles for ID 1900121' is clarified. Non-ARGO identifiers (order, customer, invoice) are not filters: use semantic_discovery.\n\n"
    "Never use an intent outside the list (QC is not an intent). parameters holds only the eight parameter names; cycles belong in cycle_numbers.\n\n"
    "Worked examples.\n  'Show cycles 10 to 20' -> {\"intent\":\"list_profiles\",\"cycle_numbers\":[10,11,12,13,14,15,16,17,18,19,20]}\n  'Show cycles 10 and 11, 12 to 15' -> {\"intent\":\"clarify\",\"clarification\":\"Give one cycle list or one cycle range; are 12 to 15 part of the range you mean?\"}\n  'Show cycles 10 to 20 sorted by highest cycle' -> {\"intent\":\"list_profiles\",\"cycle_numbers\":[10,11,12,13,14,15,16,17,18,19,20],\"sort\":{\"field\":\"cycle_number\",\"direction\":\"desc\"}}\n  'Show profiles sorted by highest cycle' -> {\"intent\":\"list_profiles\",\"sort\":{\"field\":\"cycle_number\",\"direction\":\"desc\"}}\n  'Show profiles between latitude 10 and 20' -> {\"intent\":\"clarify\",\"clarification\":\"A bounding box needs a latitude range and a longitude range. What longitude range do you want?\"}\n  'Show profiles at latitude 10 and 20 longitude 70 and 80' -> {\"intent\":\"list_profiles\",\"bounding_box\":{\"min_latitude\":10,\"max_latitude\":20,\"min_longitude\":70,\"max_longitude\":80}}\n  'Show profiles with pressure 500' -> {\"intent\":\"clarify\",\"clarification\":\"Is 500 a pressure in dbar, and is it a single value or a range?\"}\n  'Show profiles with pressure 500 dbar and pressure 500 dbar' -> {\"intent\":\"list_profiles\",\"pressure_range\":[500,500]}\n  'Show median temperature' -> {\"intent\":\"aggregate\",\"unsupported_reason\":\"median aggregation is not supported; use MEAN, COUNT, MIN or MAX\"}\n\n"
    "Unsupported requests. A well-formed request the system cannot execute (radius search, median aggregation) returns exactly two fields: intent and unsupported_reason, with no other query field. Never approximate a radius search with a bounding box.\n\n"
    "Final checks before answering. (a) Do not emit pressure_range unless the question contains 'dbar' or 'db'. (b) Do not emit cycle_numbers for a doubled or unmatched parenthesis. (c) 'adjusted'/'raw' with no parameter: emit values, never clarification, unsupported_reason or data_mode. (d) A date is never unsupported: emit the stated time_range.\n\n"
    "Question: __QUESTION__"
)

def build_qwen_prompt(question: str) -> str:
    """Return the versioned constrained prompt for one question."""

    return _PROMPT_TEMPLATE.replace("__QUESTION__", question)


class QwenInterpreter:
    """Strict local Ollama/Qwen3 interpreter.

    ``client`` is injectable for tests and must expose ``generate``; the real
    client is the existing ``ollama`` package used by the production backend.
    No fallback is invoked here.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client=None,
        generation_options: dict | None = None,
        constrained: bool = False,
    ):
        self.model = model
        self._client_override = client
        self.constrained = bool(constrained)
        self.generation_options = dict(
            GENERATION_OPTIONS if generation_options is None else generation_options
        )

    @property
    def generation_mode(self) -> str:
        return "json_schema" if self.constrained else "json"

    @property
    def name(self) -> str:
        return "qwen3"

    def _client(self):
        if self._client_override is not None:
            return self._client_override
        import ollama

        return ollama

    def is_available(self) -> bool:
        """Best-effort model availability check; never raises."""

        client = self._client()
        if not hasattr(client, "list"):
            return True
        try:
            listing = client.list()
        except Exception:
            return False
        models = getattr(listing, "models", None)
        if models is None and isinstance(listing, dict):
            models = listing.get("models", [])
        names = []
        for item in models or []:
            name = getattr(item, "model", None) or getattr(item, "name", None)
            if name:
                names.append(name)
        return any(self.model == name or self.model in name for name in names)

    def interpret(self, question: str) -> Interpretation:
        """Call Qwen3 and return a validated, non-executed interpretation."""

        response_format = "json"
        if self.constrained:
            from query_interpreter_schema import qwen_response_schema

            response_format = qwen_response_schema()

        try:
            client = self._client()
            response = client.generate(
                model=self.model,
                prompt=build_qwen_prompt(question),
                format=response_format,
                stream=False,
                think=False,
                options=self.generation_options,
            )
        except Exception as exc:
            code = _classify_ollama_exception(exc)
            return _failure_interpretation(
                code=code,
                message=f"Qwen3 request failed ({code}); no query was interpreted.",
                model_name=self.model,
                telemetry=InterpreterTelemetry(),
            )

        raw_response = _extract_raw_response(response)
        raw = raw_response.strip()
        if not raw:
            return _failure_interpretation(
                code=InterpreterFailure.EMPTY_RESPONSE.value,
                message="Qwen3 returned an empty response.",
                model_name=self.model,
                telemetry=InterpreterTelemetry(raw_response=raw_response),
            )

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return _failure_interpretation(
                code=InterpreterFailure.INVALID_JSON.value,
                message=f"Qwen3 returned invalid JSON: {exc}",
                model_name=self.model,
                telemetry=InterpreterTelemetry(
                    raw_response=raw_response,
                    json_parse_ok=False,
                    json_error=str(exc),
                ),
            )

        try:
            decoded = decode_qwen_output(payload)
        except QwenOutputError as exc:
            return _failure_interpretation(
                code=exc.code,
                message=exc.message,
                model_name=self.model,
                telemetry=InterpreterTelemetry(
                    raw_response=raw_response,
                    json_parse_ok=True,
                    decoder_accepted=False,
                    decoder_error_code=exc.code,
                    decoder_error_message=exc.message,
                    rejected_field=exc.field,
                    expected_type=exc.expected_type,
                ),
            )

        accepted_telemetry = InterpreterTelemetry(
            raw_response=raw_response,
            json_parse_ok=True,
            decoder_accepted=True,
        )

        if decoded.clarification is not None:
            return Interpretation(
                interpreter="qwen3",
                validation=ValidationResult(
                    status=ValidationStatus.NEEDS_CLARIFICATION,
                    reason=decoded.clarification,
                ),
                diagnostics=(
                    Diagnostic(
                        code="clarification",
                        message=decoded.clarification,
                    ),
                ),
                model_name=self.model,
                raw_output_valid=True,
                telemetry=accepted_telemetry,
            )

        if decoded.unsupported_reason is not None:
            return Interpretation(
                interpreter="qwen3",
                validation=ValidationResult(
                    status=ValidationStatus.UNSUPPORTED,
                    reason=decoded.unsupported_reason,
                ),
                diagnostics=(
                    Diagnostic(
                        code="unsupported",
                        message=decoded.unsupported_reason,
                    ),
                ),
                model_name=self.model,
                raw_output_valid=True,
                telemetry=accepted_telemetry,
            )

        candidate = decoded.spec
        validation = validate_spec(candidate)
        diagnostics = (
            Diagnostic(
                code="validation",
                message=validation.reason or validation.status.value,
            ),
        )
        if validation.status is ValidationStatus.VALID:
            return Interpretation(
                interpreter="qwen3",
                validation=validation,
                candidate_spec=candidate,
                diagnostics=(Diagnostic(code="schema", message="structured output accepted"),),
                model_name=self.model,
                raw_output_valid=True,
                telemetry=accepted_telemetry,
            )

        return Interpretation(
            interpreter="qwen3",
            validation=validation,
            candidate_spec=candidate,
            diagnostics=diagnostics,
            model_name=self.model,
            raw_output_valid=True,
            failure_code=InterpreterFailure.VALIDATION_FAILED.value,
            telemetry=accepted_telemetry,
        )


def interpret_with_fallback(
    question: str,
    primary,
    fallback: Callable[[str], Interpretation],
) -> Interpretation:
    """Run a fallback only when the caller explicitly supplies it.

    The fallback is never automatic inside either interpreter.  The returned
    result records the original interpreter, original status and reason.
    """

    first = primary.interpret(question)
    if first.status is ValidationStatus.VALID:
        return first

    second = fallback(question)
    return Interpretation(
        interpreter=second.interpreter,
        validation=second.validation,
        candidate_spec=second.candidate_spec,
        diagnostics=second.diagnostics,
        fallback_used=True,
        fallback_reason=(
            f"{first.interpreter} returned {first.status.value}: "
            f"{first.validation.reason or 'no detail'}"
        ),
        model_name=second.model_name,
        raw_output_valid=second.raw_output_valid,
        failure_code=second.failure_code,
        original_interpreter=first.interpreter,
        original_status=first.status,
        telemetry=second.telemetry,
    )
