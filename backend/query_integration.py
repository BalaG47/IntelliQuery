"""Phase 4B: deterministic QuerySpec pipeline inside the chat workflow.

The production chat route keeps ``ArgoQueryRouter`` as its first decision
point.  This module is the second step, and it is deliberately narrow:

* it is only consulted for questions the router would otherwise answer with
  ChromaDB semantic retrieval (exact MySQL routes keep their existing code);
* the deterministic pipeline runs at most once per question, and only for
  structured intents the planner/executor demonstrably support;
* when the pipeline cannot produce a safe answer the caller receives ``None``
  and the existing semantic + Qwen workflow runs unchanged;
* a clarification or an invalid request is reported explicitly instead of
  being silently replaced with a plausible-looking answer.

Nothing here writes SQL by hand: ``run_pipeline`` owns parsing, validation,
planning and execution, and it never executes a plan it cannot represent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from query_parser import parse_query
from query_pipeline import PipelineResult, PipelineStatus, run_pipeline
from query_spec import CORE_PARAMETERS, Intent, ValidationStatus


class DeterministicPipelineError(RuntimeError):
    """The deterministic pipeline was selected but could not execute.

    Callers must surface this as a retrieval failure; an execution failure is
    not an empty result.
    """


#: Structured intents whose execution the planner implements.  ``COMPARE`` and
#: ``SEMANTIC_DISCOVERY`` are intentionally absent: comparison needs
#: post-processing the executor does not have, and semantic discovery belongs
#: to ChromaDB.
OWNED_INTENTS = frozenset(
    {
        Intent.ENUMERATE_FLOATS,
        Intent.COUNT_PROFILES,
        Intent.LIST_PROFILES,
        Intent.PROFILE_DETAIL,
        Intent.PARAMETER_AVAILABILITY,
        Intent.METADATA_INFO,
        Intent.AGGREGATE,
    }
)

#: Parser messages that describe a *named geographic region*.  ChromaDB answers
#: "Arabian Sea" questions well, and replacing them with "please give explicit
#: coordinates" would be a regression, so they keep the semantic route.
_NAMED_REGION_MARKER = "named ocean region"

#: Intents whose clarification or rejection must reach the user instead of being
#: replaced by semantic retrieval.  ``SEMANTIC_DISCOVERY`` is deliberately
#: absent: a discovery question has no structured constraint to reject.
_REFUSAL_INTENTS = OWNED_INTENTS | {Intent.CLARIFY, Intent.COMPARE}

#: Keys forwarded to the browser for the existing "Returned profiles" table.
#: Per-level measurement arrays are never forwarded: they are large and the
#: response contract only describes profile metadata.
_PROFILE_PAYLOAD_KEYS = (
    "profile_id",
    "wmo_number",
    "cycle_number",
    "profile_time",
    "latitude",
    "longitude",
    "data_mode",
    "level_count",
)


@dataclass(frozen=True)
class DeterministicAnswer:
    """A deterministic answer already shaped for the chat response contract."""

    answer: str
    rows: tuple = ()
    status: str = "executed"
    result_shape: str | None = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def answer_deterministically(question: str, engine, *, strict: bool = False):
    """Answer ``question`` deterministically, or return ``None`` to fall back.

    A ``None`` return means "not handled here" and guarantees that no SQL was
    executed, so the caller can safely run the legacy route without producing
    a second query.  A selected-but-failed execution raises
    :class:`DeterministicPipelineError` instead of returning a wrong answer.

    ``strict`` is used when the caller knows the question carries a structured
    constraint the legacy route would silently drop (see
    :func:`has_structured_refinement`).  In that case an unsupported plan is
    reported explicitly instead of falling back to an answer that cannot
    honour the request.
    """

    result = run_pipeline(question, engine)

    intent = result.parsed.intent
    refuses = intent is not None and intent in _REFUSAL_INTENTS

    # A refusal is information, not a fallback trigger: the parser has already
    # decided the question cannot be executed as written.  Named regions are
    # the one exception, because semantic retrieval handles them well.
    if refuses and result.status is PipelineStatus.NEEDS_CLARIFICATION:
        if _NAMED_REGION_MARKER in (result.message or ""):
            return None
        return DeterministicAnswer(
            _clarification(result.message),
            status="needs_clarification",
        )

    if refuses and result.status is PipelineStatus.INVALID:
        return DeterministicAnswer(
            _invalid(result.message),
            status="invalid",
        )

    is_bgc_compare = (
        intent is Intent.COMPARE
        and result.plan is not None
        and result.plan.result_shape.value == "bgc_calculation"
    )
    if intent is None or (intent not in OWNED_INTENTS and not is_bgc_compare):
        return None

    # Unsupported plans (depth, pressure ranges, MEAN/MIN/MAX, ...) and
    # no-database intents are left to the existing workflow, unless the caller
    # needs an explicit statement that the constraint was not applied.
    if result.status is PipelineStatus.UNSUPPORTED:
        if strict:
            return DeterministicAnswer(
                _unsupported(result.message),
                status="unsupported",
            )
        return None
    if result.status is PipelineStatus.NO_DATABASE_QUERY:
        return None

    if result.status is PipelineStatus.EXECUTION_ERROR:
        logging.error(
            "Deterministic query execution failed after selection (%s).",
            result.error or "unknown error",
        )
        raise DeterministicPipelineError(
            result.error or "The structured query could not be executed."
        )

    return _render(result)


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def has_structured_refinement(question: str) -> bool:
    """True when a WMO question carries more than the identifier itself.

    The legacy WMO routes understand the identifier, "latest" wording and an
    explicit N, but nothing else: a date, cycle, area, data mode, QC filter, a
    second WMO or a parameter presence request would be silently dropped.  The
    caller uses this to hand such questions to the deterministic pipeline
    instead of the legacy branch.

    A core-parameter mention alone ("Show temperature for WMO ...") is the
    legacy detail case and stays there; a BGC parameter (oxygen, chlorophyll,
    ...) cannot be answered by the legacy branch at all.
    """

    parsed = parse_query(question)
    spec = parsed.validation.spec

    if spec is None:
        # An invalid or clarifying parse also means the legacy branch cannot
        # honour the question, so the refusal must be surfaced.  The same is
        # true for an unsupported operation (median, percentile, ...): falling
        # back to retrieval would answer a different question.
        return (
            parsed.status is ValidationStatus.INVALID
            or parsed.intent is Intent.CLARIFY
            or (
                parsed.status is ValidationStatus.UNSUPPORTED
                and parsed.intent is not Intent.SEMANTIC_DISCOVERY
            )
        )

    if len(spec.wmo_numbers) > 1:
        return True

    if (
        spec.cycle_numbers
        or spec.time_range
        or spec.bounding_box
        or spec.qc_filter
        or spec.data_mode
        or spec.has_bgc is not None
        or spec.direction is not None
        or spec.float_level
        or spec.numeric_filter is not None
        or spec.pressure_range
        or spec.depth_requested
        or spec.depth_range_m
        or spec.aggregations
        or spec.group_by
    ):
        return True

    return any(
        parameter not in CORE_PARAMETERS for parameter in spec.parameters
    )


def _clarification(reason: str | None) -> str:
    detail = reason or "The question is missing a value needed to query the data."
    return (
        "I need a little more detail before querying the data: "
        f"{detail}"
    )


def _invalid(reason: str | None) -> str:
    detail = reason or "The request could not be interpreted safely."
    return (
        "I could not run that query: "
        f"{detail} No data was retrieved, so nothing was guessed."
    )


def _unsupported(reason: str | None) -> str:
    detail = reason or "One of the requested filters cannot be applied exactly."
    return (
        "I cannot apply every part of that request to the data: "
        f"{detail} No data was retrieved, so nothing was guessed."
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural if plural is not None else singular + "s"


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _level_count(value) -> int | None:
    """Count the levels in a stored measurement array without inventing one."""

    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return len(parsed) if isinstance(parsed, list) else None
    return None


def _profile_payload(rows) -> tuple:
    payload = []
    for row in rows:
        payload.append(
            {
                key: row.get(key)
                for key in _PROFILE_PAYLOAD_KEYS
                if key in row
            }
        )
    return tuple(payload)


def _sort_note(result: PipelineResult) -> str:
    spec = result.parsed.spec
    if spec is None or spec.sort is None:
        return ""
    field = spec.sort.field.value.replace("_", " ")
    direction = spec.sort.direction.value
    return f" Ordered by {field} ({direction})."


def _limit_note(result: PipelineResult, returned: int) -> str:
    limit = result.plan.limit if result.plan is not None else None
    if limit is None or returned < limit:
        return ""
    return (
        f" Showing the first {returned}; more records match the request "
        "but were not returned."
    )


# ---------------------------------------------------------------------------
# Result rendering (deterministic prose, no language model)
# ---------------------------------------------------------------------------


def _render(result: PipelineResult) -> DeterministicAnswer | None:
    shape = result.result_shape
    rows = result.rows

    if shape == "profile_rows":
        return _render_profile_rows(result, rows)
    if shape == "profile_detail":
        return _render_profile_detail(result, rows)
    if shape == "count":
        return _render_count(result, rows)
    if shape == "count_by_group":
        return _render_count_by_group(result, rows)
    if shape == "float_rows":
        return _render_float_rows(rows)
    if shape == "float_metadata":
        return _render_float_metadata(rows)
    if shape == "availability_rows":
        return _render_availability(result, rows)
    if shape == "bgc_calculation":
        ans = _render_bgc_calculation(result, rows)
        return DeterministicAnswer(ans.answer, ans.rows, ans.status, result_shape=shape)

    # An unknown shape is not a success: let the existing route answer.
    logging.warning(
        "Deterministic result shape %r is not renderable; falling back.",
        shape,
    )
    return None


def _render_profile_rows(result: PipelineResult, rows) -> DeterministicAnswer:
    count = len(rows)
    if count == 0:
        return DeterministicAnswer(
            "No profiles matched the requested filters.",
            (),
        )

    text = (
        f"Found {count} {_plural(count, 'profile')} matching the request."
        + _sort_note(result)
        + _limit_note(result, count)
    )
    return DeterministicAnswer(text, _profile_payload(rows))


def _render_profile_detail(result: PipelineResult, rows) -> DeterministicAnswer:
    if not rows:
        return DeterministicAnswer(
            "No profile matched the requested identifiers or filters.",
            (),
        )

    row = rows[0]
    heading = f"Profile {row.get('profile_id')}"
    identifiers = []
    if row.get("wmo_number") is not None:
        identifiers.append(f"WMO {row['wmo_number']}")
    if row.get("cycle_number") is not None:
        identifiers.append(f"cycle {row['cycle_number']}")
    if identifiers:
        heading += " (" + ", ".join(identifiers) + ")"

    sentence = heading
    if row.get("profile_time") is not None:
        sentence += f" was recorded on {row['profile_time']}"
    if row.get("latitude") is not None and row.get("longitude") is not None:
        sentence += (
            f" at latitude {row['latitude']}, longitude {row['longitude']}"
        )
    sentence += "."

    if row.get("data_mode") is not None:
        sentence += f" Data mode: {row['data_mode']}."

    levels = []
    for key in ("pressure", "temperature", "salinity"):
        if key not in row:
            continue
        count = _level_count(row.get(key))
        if count is not None:
            levels.append(f"{key} {count}")
    if levels:
        sentence += " Stored levels: " + ", ".join(levels) + "."

    return DeterministicAnswer(sentence, _profile_payload(rows))


def _render_count(result: PipelineResult, rows) -> DeterministicAnswer:
    count = _as_int(rows[0].get("profile_count")) if rows else 0
    verb = "is" if count == 1 else "are"
    text = (
        f"There {verb} {count} {_plural(count, 'profile')} matching the "
        "request."
    )
    return DeterministicAnswer(text, ())


def _render_count_by_group(result: PipelineResult, rows) -> DeterministicAnswer:
    if not rows:
        return DeterministicAnswer(
            "No profiles matched the requested filters.",
            (),
        )

    total = sum(_as_int(row.get("profile_count")) for row in rows)
    spec = result.parsed.spec
    grouping = spec.group_by.value if spec is not None and spec.group_by else None
    heading = f"Total matching profiles: {total}"
    if grouping:
        heading += f", grouped by {grouping.replace('_', ' ')}"
    heading += "."

    lines = [
        f"- {row.get('group_key')}: {_as_int(row.get('profile_count'))} "
        f"{_plural(_as_int(row.get('profile_count')), 'profile')}"
        for row in rows
    ]
    text = heading + _limit_note(result, len(rows)) + "\n" + "\n".join(lines)
    return DeterministicAnswer(text, ())


def _render_float_rows(rows) -> DeterministicAnswer:
    count = len(rows)
    if count == 0:
        return DeterministicAnswer(
            "No floats are stored in the connected database.",
            (),
        )

    wmos = [str(row.get("wmo_number")) for row in rows]
    shown = wmos[:25]
    listing = ", ".join(shown)
    if count > len(shown):
        listing += f" and {count - len(shown)} more"
    return DeterministicAnswer(
        f"Found {count} {_plural(count, 'float')}: {listing}.",
        (),
    )


def _render_float_metadata(rows) -> DeterministicAnswer:
    count = len(rows)
    if count == 0:
        return DeterministicAnswer(
            "No floats matched the requested identifiers.",
            (),
        )

    lines = []
    for row in rows:
        profiles = _as_int(row.get("profile_count"))
        descriptor = []
        if row.get("project_name"):
            descriptor.append(str(row["project_name"]))
        if row.get("platform_type"):
            descriptor.append(str(row["platform_type"]))
        suffix = f" ({', '.join(descriptor)})" if descriptor else ""
        lines.append(
            f"- WMO {row.get('wmo_number')}{suffix}: {profiles} "
            f"{_plural(profiles, 'profile')}"
        )
    return DeterministicAnswer(
        f"Found {count} {_plural(count, 'float')}:\n" + "\n".join(lines),
        (),
    )


def _render_availability(result: PipelineResult, rows) -> DeterministicAnswer:
    count = len(rows)
    if count == 0:
        return DeterministicAnswer(
            "No profiles matched the requested filters, so availability could "
            "not be checked.",
            (),
        )

    keys = [key for key in rows[0] if key.startswith("has_")]
    if not keys:
        return DeterministicAnswer(
            "Availability was checked but no parameter columns were returned.",
            (),
        )

    lines = []
    for key in keys:
        parameter = key[len("has_"):]
        present = sum(1 for row in rows if row.get(key))
        lines.append(
            f"- {parameter}: {present} of {count} matching "
            f"{_plural(count, 'profile')}"
        )
    note = _limit_note(result, count).strip()
    text = (
        f"Parameter availability for {count} matching "
        f"{_plural(count, 'profile')}:\n"
        + "\n".join(lines)
        + (f"\n{note}" if note else "")
    )
    return DeterministicAnswer(text, ())


def _render_bgc_calculation(result: PipelineResult, rows) -> DeterministicAnswer:
    if not rows:
        return DeterministicAnswer(
            "No matching BGC profiles were found for the requested calculation.",
            (),
        )

    lines = []
    for row in rows:
        param = row.get("parameter", "BGC")
        unit = row.get("unit", "")
        valid = row.get("valid_count", 0)
        missing = row.get("missing_count", 0)
        excluded = row.get("excluded_count", 0)
        scope = row.get("aggregation_scope", "result_set")
        wmo = row.get("wmo_number")
        cycle = row.get("cycle_number")

        prefix = f"{param}"
        if wmo is not None and cycle is not None:
            prefix += f" (WMO {wmo}, cycle {cycle})"
        elif wmo is not None:
            prefix += f" (WMO {wmo})"
        else:
            prefix += f" ({scope})"

        if valid == 0:
            lines.append(
                f"{prefix}: no valid measurements found "
                f"(missing={missing}, qc_excluded={excluded}, policy={row.get('qc_policy')})."
            )
            continue

        unit_str = f" {unit}" if unit else ""
        lines.append(
            f"{prefix}: mean={row.get('mean')}{unit_str}, "
            f"median={row.get('median')}{unit_str}, "
            f"min={row.get('minimum')}{unit_str}, "
            f"max={row.get('maximum')}{unit_str}, "
            f"std_dev={row.get('std_dev')} "
            f"(valid={valid}, missing={missing}, qc_excluded={excluded}, "
            f"mode={row.get('data_mode')}, qc={row.get('qc_policy')})."
        )

    return DeterministicAnswer("\n".join(lines), ())
