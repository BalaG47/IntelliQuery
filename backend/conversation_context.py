"""Minimal, stateless conversation context (Phase 4D).

The chat route has no session store.  Instead, the client echoes back a small
validated filter object (``context``) from the previous structured answer, and
this module merges a recognised follow-up into a self-contained question that
goes through the normal parser/planner.  Nothing here executes SQL, calls a
model or trusts client text: the merged question is parsed and validated again.

Recognised follow-ups:

* ``How many are there?``          -> count with the retained filters
* ``What about 2024?``             -> replace the year
* ``Now cycle 25.``                -> replace the cycle filter
* ``Only delayed mode.``           -> add/replace the data mode
* ``What about temperature?``      -> add a parameter
* ``Without delayed mode.``        -> remove that filter

Anything else is a new question and receives no context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import re

from query_parser import parse_query
from query_spec import (
    DataMode,
    Intent,
    Parameter,
    ValidationStatus,
)

#: Intents whose filters are worth carrying to the next turn.
_CONTEXT_INTENTS = frozenset(
    {
        Intent.LIST_PROFILES,
        Intent.PROFILE_DETAIL,
        Intent.COUNT_PROFILES,
        Intent.AGGREGATE,
        Intent.PARAMETER_AVAILABILITY,
        Intent.METADATA_INFO,
    }
)

_PARAMETER_WORDS = {
    Parameter.TEMP: "temperature",
    Parameter.PSAL: "salinity",
    Parameter.PRES: "pressure",
    Parameter.DOXY: "oxygen",
    Parameter.CHLA: "chlorophyll",
    Parameter.NITRATE: "nitrate",
    Parameter.PH_IN_SITU_TOTAL: "ph",
}

_PARAMETER_ALIASES = {
    "temperature": Parameter.TEMP,
    "temp": Parameter.TEMP,
    "salinity": Parameter.PSAL,
    "salt": Parameter.PSAL,
    "pressure": Parameter.PRES,
    "oxygen": Parameter.DOXY,
    "doxy": Parameter.DOXY,
    "chlorophyll": Parameter.CHLA,
    "chl": Parameter.CHLA,
    "nitrate": Parameter.NITRATE,
    "ph": Parameter.PH_IN_SITU_TOTAL,
}


@dataclass(frozen=True)
class FollowUp:
    """Outcome of trying to interpret a prompt against prior context."""

    question: str | None = None
    clarification: str | None = None
    context: dict | None = None


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------


def extract_context(question: str) -> dict | None:
    """Return the reusable filters of a parsed structured question.

    ``None`` means the question does not describe a supported structured
    query, so the next turn starts fresh.
    """

    parsed = parse_query(question)

    if parsed.status is not ValidationStatus.VALID:
        return None
    if parsed.intent not in _CONTEXT_INTENTS:
        return None

    spec = parsed.validation.spec
    if spec is None:
        return None

    context = {
        "wmo_numbers": [int(value) for value in spec.wmo_numbers],
        "cycle_numbers": [int(value) for value in spec.cycle_numbers],
        # Open-ended ranges ("after 2021") have no second bound; they are not
        # carried, because a follow-up must not invent an end date.
        "time_start": (
            spec.time_range.start.date().isoformat()
            if spec.time_range is not None
            and spec.time_range.start is not None
            and spec.time_range.end is not None
            else None
        ),
        "time_end": (
            spec.time_range.end.date().isoformat()
            if spec.time_range is not None
            and spec.time_range.start is not None
            and spec.time_range.end is not None
            else None
        ),
        "bounding_box": (
            {
                "min_latitude": float(spec.bounding_box.min_latitude),
                "max_latitude": float(spec.bounding_box.max_latitude),
                "min_longitude": float(spec.bounding_box.min_longitude),
                "max_longitude": float(spec.bounding_box.max_longitude),
            }
            if spec.bounding_box is not None
            else None
        ),
        "data_mode": spec.data_mode.value if spec.data_mode is not None else None,
        "parameters": [parameter.value for parameter in spec.parameters],
    }

    if not any(
        context[key]
        for key in (
            "wmo_numbers",
            "cycle_numbers",
            "time_start",
            "bounding_box",
            "data_mode",
            "parameters",
        )
    ):
        return None

    return context


def sanitise_context(raw) -> dict | None:
    """Accept only well-typed, bounded context fields from a client."""

    if not isinstance(raw, dict):
        return None

    def _ints(values, low, high, limit=10):
        if not isinstance(values, list):
            return []
        result = []
        for value in values[:limit]:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if low <= value <= high:
                result.append(value)
        return result

    def _date(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            return None
        if not (1990 <= parsed.year <= 2100):
            return None
        return parsed.isoformat()

    def _float(value, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not (low <= value <= high):
            return None
        return value

    context = {
        "wmo_numbers": _ints(raw.get("wmo_numbers"), 1_000_000, 9_999_999),
        "cycle_numbers": _ints(raw.get("cycle_numbers"), 0, 9999),
        "time_start": _date(raw.get("time_start")),
        "time_end": _date(raw.get("time_end")),
        "bounding_box": None,
        "data_mode": raw.get("data_mode") if raw.get("data_mode") in {"D", "R"} else None,
        "parameters": [
            value
            for value in (raw.get("parameters") or [])[:8]
            if isinstance(value, str) and value in {p.value for p in Parameter}
        ],
    }

    box = raw.get("bounding_box")
    if isinstance(box, dict):
        values = {
            "min_latitude": _float(box.get("min_latitude"), -90, 90),
            "max_latitude": _float(box.get("max_latitude"), -90, 90),
            "min_longitude": _float(box.get("min_longitude"), -180, 180),
            "max_longitude": _float(box.get("max_longitude"), -180, 180),
        }
        if (
            None not in values.values()
            and values["min_latitude"] <= values["max_latitude"]
            and values["min_longitude"] <= values["max_longitude"]
        ):
            context["bounding_box"] = values

    if context["time_start"] and context["time_end"]:
        if context["time_start"] > context["time_end"]:
            context["time_start"] = context["time_end"] = None
    elif context["time_start"] or context["time_end"]:
        context["time_start"] = context["time_end"] = None

    if not any(
        context[key]
        for key in (
            "wmo_numbers",
            "cycle_numbers",
            "time_start",
            "bounding_box",
            "data_mode",
            "parameters",
        )
    ):
        return None

    return context


# ---------------------------------------------------------------------------
# Follow-up merging
# ---------------------------------------------------------------------------


def merge_followup(raw_context, prompt: str) -> FollowUp | None:
    """Merge ``prompt`` into ``raw_context``.

    ``None`` means the prompt is not a recognised follow-up; the caller treats
    it as a new question.  A ``FollowUp`` with ``clarification`` set means the
    prompt is a follow-up that cannot be combined safely.
    """

    context = sanitise_context(raw_context)
    if context is None:
        return None

    text = " ".join((prompt or "").lower().split()).strip()

    if not text:
        return None

    count_requested = False
    changed = False

    # 1. Count of the previously described set.
    if re.fullmatch(r"(?:and\s+)?how many(?:\s+(?:profiles|records))?(?:\s+are there|\s+in total)?\??", text):
        count_requested = True
        changed = True

    # 2. Removal of a retained filter ("without delayed mode", "remove the date").
    elif re.search(r"\bwithout\s+(?:the\s+)?delayed[- ]mode\b|\bnot\s+delayed[- ]mode\b", text):
        context["data_mode"] = None
        changed = True
    elif re.search(r"\bwithout\s+(?:the\s+)?real[- ]time\b|\bnot\s+real[- ]time\b", text):
        context["data_mode"] = None
        changed = True
    elif re.search(r"\b(?:remove|drop|clear)\s+(?:the\s+)?(?:date|year|time)\b", text):
        context["time_start"] = context["time_end"] = None
        changed = True
    elif re.search(r"\b(?:remove|drop|clear)\s+(?:the\s+)?cycles?\b", text):
        context["cycle_numbers"] = []
        changed = True

    # Phase 4E: removing a retained parameter filter.  Without this the
    # follow-up was treated as a fresh question and a "remove the temperature
    # filter" request came back with temperature profiles.
    elif _parameter_removal(text) is not None:
        parameter = _parameter_removal(text)
        name = _PARAMETER_WORDS[parameter]
        if parameter.value not in context["parameters"]:
            return FollowUp(
                clarification=(
                    f"The previous filters do not include {name}, so there is "
                    "nothing to remove. State the request you want instead "
                    "(for example 'show profiles from 2024')."
                )
            )
        context["parameters"].remove(parameter.value)
        changed = True

    # 3. Year replacement / narrowing ("what about 2024?", "limit them to 2023").
    else:
        year = re.search(r"\b((?:19|20)\d{2})\b", text)

        if year and re.search(
            r"\b(?:what about|and|only|in|during|for|limit(?:\s+them)?\s+to|"
            r"just|now|restrict(?:\s+them)?\s+to)\b",
            text,
        ):
            start = date(int(year.group(1)), 1, 1)
            context["time_start"] = start.isoformat()
            context["time_end"] = (
                date(start.year, 12, 31)
            ).isoformat()
            changed = True

        # 4. Cycle replacement ("now cycle 25").
        elif re.search(r"\b(?:now\s+)?cycles?\s+\d{1,4}\b", text):
            values = [
                int(value)
                for value in re.findall(r"\bcycles?\s+(\d{1,4})\b", text)
            ]
            if values:
                context["cycle_numbers"] = values
                changed = True

        # 5. Data mode ("only delayed mode").
        elif re.search(r"\bdelayed[- ]mode\b|\bmode\s+d\b", text):
            context["data_mode"] = DataMode.DELAYED.value
            changed = True
        elif re.search(r"\breal[- ]time\b|\bmode\s+r\b", text):
            context["data_mode"] = DataMode.REAL_TIME.value
            changed = True

        # 6. Parameter ("what about temperature?").
        else:
            parameter = _parameter_followup(text)
            if parameter is not None:
                if parameter.value not in context["parameters"]:
                    context["parameters"].append(parameter.value)
                changed = True

    if not changed:
        # A short "what about ..." that matched no supported constraint is
        # ambiguous, not a new dataset question.
        if re.match(r"^(?:and\s+)?(?:what|how)\s+about\b", text) and len(text.split()) <= 5:
            return FollowUp(
                clarification=(
                    "I could not tell which constraint that follow-up changes. "
                    "Give a year, a cycle number, a data mode or a parameter "
                    "(for example 'what about 2024?')."
                )
            )
        return None

    question = _build_question(context, count=count_requested)
    parsed = parse_query(question)

    if parsed.status is not ValidationStatus.VALID:
        return FollowUp(
            clarification=(
                "I could not combine that follow-up with the previous filters "
                "safely. Please state the complete request (for example "
                "'show delayed-mode profiles from 2024')."
            )
        )

    return FollowUp(
        question=question,
        context=extract_context(question) or context,
    )


def _parameter_removal(text: str) -> Parameter | None:
    """Parameter named by a removal instruction, if any."""

    match = re.search(
        r"\b(?:remove|drop|clear|without)\s+(?:the\s+)?"
        r"(temperature|temp|salinity|salt|pressure|oxygen|doxy|chlorophyll|"
        r"chl|nitrate|ph)\b(?:\s+filter)?",
        text,
    )
    if match is None:
        return None
    return _PARAMETER_ALIASES.get(match.group(1))


def _parameter_followup(text: str) -> Parameter | None:
    match = re.search(
        r"\b(?:what about|and|how about|only|with)\s+"
        r"(temperature|temp|salinity|salt|pressure|oxygen|doxy|chlorophyll|"
        r"chl|nitrate|ph)\b",
        text,
    )
    if match is None:
        return None
    return _PARAMETER_ALIASES.get(match.group(1))


def _build_question(context: dict, *, count: bool) -> str:
    """Compose a self-contained question from validated filters."""

    parts = ["How many profiles" if count else "Show profiles"]

    if context.get("wmo_numbers"):
        parts.append(
            "from "
            + " and ".join(f"WMO {value}" for value in context["wmo_numbers"])
        )

    if context.get("cycle_numbers"):
        values = ", ".join(str(value) for value in context["cycle_numbers"])
        parts.append(f"with cycles {values}")

    if context.get("time_start") and context.get("time_end"):
        parts.append(
            f"from {context['time_start']} to {context['time_end']}"
        )

    box = context.get("bounding_box")
    if box:
        parts.append(
            "between latitude "
            f"{box['min_latitude']} and {box['max_latitude']} longitude "
            f"{box['min_longitude']} and {box['max_longitude']}"
        )

    if context.get("data_mode") == DataMode.DELAYED.value:
        parts.append("with delayed mode")
    elif context.get("data_mode") == DataMode.REAL_TIME.value:
        parts.append("with real-time mode")

    for value in context.get("parameters") or []:
        try:
            parameter = Parameter(value)
        except ValueError:
            continue
        parts.append(f"with {_PARAMETER_WORDS[parameter]}")

    return " ".join(parts) + "?"
