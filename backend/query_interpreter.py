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
PROMPT_VERSION = "phase4a-v1"
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

    @property
    def status(self) -> ValidationStatus:
        return self.validation.status


class QwenOutputError(ValueError):
    """A strict-decoder rejection with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


def _fail(code: str, message: str) -> QwenOutputError:
    return QwenOutputError(code, message)


def _string(value, field: str) -> str:
    if not isinstance(value, str):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be a string.")
    return value


def _nonempty_string(value, field: str) -> str:
    text = _string(value, field)
    if not text.strip():
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must not be empty.")
    return text


def _int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be an integer.")
    return value


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be a number.")
    return float(value)


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be a boolean.")
    return value


def _list(value, field: str) -> list:
    if not isinstance(value, list):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be a list.")
    return value


def _pair(value, field: str) -> tuple[float, float]:
    items = _list(value, field)
    if len(items) != 2:
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must have two values.")
    return (_number(items[0], f"{field}[0]"), _number(items[1], f"{field}[1]"))


def _enum(value, enum_type, field: str):
    if not isinstance(value, str):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be a string.")
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} has invalid value {value!r}; expected one of: {allowed}.",
        )


def _datetime(value, field: str) -> datetime:
    text = _string(value, field)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} must be an ISO-8601 date or datetime.",
        )


def _object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, f"{field} must be an object.")
    return value


def _check_keys(value: dict, allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            f"{field} has unexpected field(s): {', '.join(sorted(unknown))}.",
        )


def decode_qwen_output(payload: dict) -> DecodedQwenOutput:
    """Strictly convert one Qwen JSON object into a candidate or outcome.

    The function never validates a QuerySpec; the caller must do that.  It only
    ensures the output has the approved shape and types.
    """

    if not isinstance(payload, dict):
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, "Output must be a JSON object.")

    for key in payload:
        if not isinstance(key, str):
            raise _fail(InterpreterFailure.INVALID_SCHEMA.value, "Field names must be strings.")
        normalised = key.lower()
        if normalised in _FORBIDDEN_KEYS:
            raise _fail(
                InterpreterFailure.FORBIDDEN_SQL.value,
                f"Model output contains forbidden field {key!r}.",
            )
        if key not in _ALLOWED_KEYS:
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                f"Unexpected field {key!r}.",
            )

    if "intent" not in payload:
        raise _fail(InterpreterFailure.INVALID_SCHEMA.value, "Missing required field 'intent'.")

    intent = _enum(payload["intent"], Intent, "intent")

    has_clarification = "clarification" in payload
    has_unsupported = "unsupported_reason" in payload

    if has_clarification and has_unsupported:
        raise _fail(
            InterpreterFailure.INVALID_SCHEMA.value,
            "Output cannot contain both clarification and unsupported_reason.",
        )

    if has_clarification or has_unsupported:
        if any(key in payload for key in _QUERY_FIELDS):
            raise _fail(
                InterpreterFailure.INVALID_SCHEMA.value,
                "A clarification or unsupported result cannot carry query fields.",
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


def _extract_response_text(response) -> str:
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        value = response.get("response", "")
        return value.strip() if isinstance(value, str) else ""
    value = getattr(response, "response", "")
    return value.strip() if isinstance(value, str) else ""


def build_qwen_prompt(question: str) -> str:
    """Return the versioned constrained prompt for one question."""

    return (
        "You are a structured query interpreter for an ARGO oceanographic "
        "database. Return exactly one JSON object and no Markdown or prose.\n\n"
        "You must not return SQL, table names, column names, connection "
        "details, code, tool calls or execution instructions. The downstream "
        "system validates and plans the query itself.\n\n"
        "Allowed top-level fields:\n"
        "intent, parameters, wmo_numbers, cycle_numbers, time_range, "
        "bounding_box, pressure_range, depth_range_m, depth_requested, values, "
        "qc_filter, data_mode, aggregations, group_by, sort, limit, "
        "semantic_query, notes, clarification, unsupported_reason.\n\n"
        "Use only these intent values: enumerate_floats, count_profiles, "
        "list_profiles, profile_detail, aggregate, compare, "
        "parameter_availability, metadata_info, conversational, "
        "semantic_discovery, clarify.\n"
        "Use only these parameter values: TEMP, PSAL, PRES, DOXY, CHLA, "
        "BBP700, NITRATE, PH_IN_SITU_TOTAL.\n"
        "Use values 'raw' or 'adjusted'; data_mode 'R' or 'D'; QC scope "
        "'profile' or 'measurement'; aggregation values COUNT, MEAN, MIN, MAX; "
        "group_by values year, month, wmo, profile; sort fields profile_time, "
        "cycle_number, wmo_number, latitude, longitude, profile_id; sort "
        "directions asc or desc.\n"
        "Pressure is in dbar. Never convert depth in metres into pressure; "
        "use depth_requested or depth_range_m when the user asks in depth.\n"
        "If the request is ambiguous, return {\"intent\":\"clarify\","
        "\"clarification\":\"<specific question>\"}.\n"
        "If the operation is unsupported, return {\"intent\":\"<closest intent>\","
        "\"unsupported_reason\":\"<specific reason>\"}.\n"
        "Omit absent fields. Arrays are JSON arrays. A time_range is "
        "{\"start\":\"ISO-8601\",\"end\":\"ISO-8601\"}; a bounding_box uses "
        "min_latitude, max_latitude, min_longitude, max_longitude.\n\n"
        f"Question: {question}"
    )


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
    ):
        self.model = model
        self._client_override = client
        self.generation_options = dict(
            GENERATION_OPTIONS if generation_options is None else generation_options
        )

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

        try:
            client = self._client()
            response = client.generate(
                model=self.model,
                prompt=build_qwen_prompt(question),
                format="json",
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
            )

        raw = _extract_response_text(response)
        if not raw:
            return _failure_interpretation(
                code=InterpreterFailure.EMPTY_RESPONSE.value,
                message="Qwen3 returned an empty response.",
                model_name=self.model,
            )

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return _failure_interpretation(
                code=InterpreterFailure.INVALID_JSON.value,
                message=f"Qwen3 returned invalid JSON: {exc}",
                model_name=self.model,
            )

        try:
            decoded = decode_qwen_output(payload)
        except QwenOutputError as exc:
            return _failure_interpretation(
                code=exc.code,
                message=exc.message,
                model_name=self.model,
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
            )

        return Interpretation(
            interpreter="qwen3",
            validation=validation,
            candidate_spec=candidate,
            diagnostics=diagnostics,
            model_name=self.model,
            raw_output_valid=True,
            failure_code=InterpreterFailure.VALIDATION_FAILED.value,
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
    )
