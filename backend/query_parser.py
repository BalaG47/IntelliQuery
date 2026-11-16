"""Deterministic natural-language query parser (Phase 3C-C).

The parser turns a user's question into a :class:`~query_spec.QuerySpec` and
validates it with the existing Phase 3C-B validator.  It contains **no SQL, no
database access and no LLM call**: the same text always produces the same
result, which makes it safe to run in shadow mode beside the production router.

Pipeline
--------
1. **Normalise** - Unicode NFKC, lowercase, collapse whitespace, unify degree
   signs and dashes.
2. **Extract** - parameters, WMO numbers, cycles, dates, geography, pressure,
   depth, QC, data mode, raw/adjusted, aggregations, sorting and limits.
3. **Detect intent** - an ordered rule list (documented in ``_INTENT_RULES``).
4. **Build and validate** - construct a ``QuerySpec`` and call ``validate_spec``.
5. **Annotate** - confidence, diagnostics, ambiguities and unrecognised text.

Parser-level outcomes (an ambiguous date, an unsupported aggregation, a named
ocean region) are reported as a ``ParsedQuery`` whose ``spec`` is ``None`` and
whose status is ``NEEDS_CLARIFICATION``/``UNSUPPORTED``.  Everything that can be
expressed as a specification is left to the QuerySpec validator, so its status
and reason are preserved verbatim.

Known deliberate limitations
----------------------------
* Only explicit geographic bounds are supported; named seas/basins are not
  modelled (Phase 3C-B decision, M5).
* Radius searches are parsed but remain UNSUPPORTED downstream.
* A seven-digit number is a WMO only when an identifier cue (``WMO``,
  ``float``, ``platform``, ``ID``) appears within three tokens before it or
  two tokens after it.  An uncued number is never placed in the spec; if the
  sentence reads like a platform lookup it becomes a clarification.
* Cycle semantics: ``cycles 10 and 11`` and cycle ranges populate
  ``cycle_numbers``.  Every cycle-count phrase (``first``, ``oldest``,
  ``earliest``, ``latest``, ``last``, ``newest`` N cycles) asks for
  clarification: which cycle numbers exist depends on the data, so the parser
  never guesses a numeric range and never turns the phrase into a result
  ``limit``.

  Supported cycle grammar (after ``cycle``/``cycles``, optionally ``range``):

  * one list: ``10``, ``10, 11``, ``10, 11 and 12``, ``10, 11, and 12``,
    ``10; 11``, ``10 & 11``; every value may carry ``#`` (``#10``);
  * one range: ``10 to 20``, ``10 through 20``, ``10-20``, ``#10 to #20``;
  * either of the above in one balanced pair of parentheses.

  Anything else in the same cycle clause (a second list/range, a trailing
  number, ``then cycle 25``, a nested or unbalanced parenthesis) is a
  clarification, never a partial filter.  The clause ends at a filter
  introducer such as ``for``/``with``/``from``/``between``/``at`` so date,
  parameter, QC and WMO filters are not mistaken for cycle values.
* Date ranges are inclusive of the end date (``TimeRange.end`` is end-of-day
  for a requested date).  A planner that compares against a half-open
  interval must add one day itself; the parser does not silently widen a
  requested interval.
* Anything that does not match a structured rule falls back to
  ``SEMANTIC_DISCOVERY`` with the original text, never to a fabricated filter.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from query_spec import (
    Aggregation,
    BoundingBox,
    DataMode,
    Intent,
    MeasurementValues,
    Parameter,
    QcFilter,
    QcScope,
    QuerySpec,
    RadiusSearch,
    SortDirection,
    SortField,
    SortSpec,
    TimeRange,
    ValidationResult,
    ValidationStatus,
    good_quality_filter,
    validate_spec,
)

__all__ = ["ParsedQuery", "parse_query", "CONVERSATIONAL_PHRASES"]


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedQuery:
    """Everything the parser understood about one user question."""

    text: str
    intent: Intent | None
    spec: QuerySpec | None
    validation: ValidationResult
    confidence: float
    diagnostics: tuple[str, ...] = ()
    ambiguous_terms: tuple[str, ...] = ()

    @property
    def status(self) -> ValidationStatus:
        return self.validation.status

    @property
    def reason(self) -> str | None:
        return self.validation.reason

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.VALID


# ---------------------------------------------------------------------------
# Normalisation and vocabulary
# ---------------------------------------------------------------------------

CONVERSATIONAL_PHRASES = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "hey there",
        "good morning",
        "good afternoon",
        "good evening",
        "how are you",
        "what's up",
        "whats up",
        "thanks",
        "thank you",
        "thank you so much",
        "bye",
        "goodbye",
    }
)

_REGION_TERMS = (
    "arabian sea",
    "indian ocean",
    "bay of bengal",
    "southern ocean",
    "arctic ocean",
    "atlantic ocean",
    "pacific ocean",
    "mediterranean sea",
    "red sea",
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

#: (parameter, alias pattern).  Longest aliases are matched first so
#: "dissolved oxygen" wins over "oxygen".
_PARAMETER_ALIASES = (
    (Parameter.TEMP, r"\btemperatures?\b|\bthermal\b"),
    (Parameter.PSAL, r"\bsalinit(?:y|ies)\b"),
    (Parameter.PRES, r"\bpressures?\b"),
    (Parameter.DOXY, r"\bdissolved oxygen\b|\boxygen\b"),
    (Parameter.CHLA, r"\bchlorophyll(?:-?a)?\b|\bchl-?a\b"),
    (
        Parameter.BBP700,
        r"\bbackscatter(?:ing)?\b|\boptical backscatter\b|\bbbp700\b",
    ),
    (Parameter.NITRATE, r"\bnitrates?\b"),
    (Parameter.PH_IN_SITU_TOTAL, r"\bph\b"),
)

_AGGREGATION_ALIASES = (
    (Aggregation.MEAN, r"\baverages?\b|\bmean\b|\bavg\b"),
    (Aggregation.MIN, r"\bminimum\b|\bmin\b"),
    (Aggregation.MAX, r"\bmaximum\b|\bmax\b"),
    (Aggregation.COUNT, r"\bcounts?\b"),
)

_UNSUPPORTED_AGGREGATIONS = (
    "median",
    "percentile",
    "mode value",
    "standard deviation",
    "stddev",
    "variance",
)

_COMPARE_PATTERNS = (
    r"\bcompare\b",
    r"\bcomparison\b",
    r"\bcompared to\b",
    r"\bversus\b",
    r"\bvs\.?\b",
    r"\bdifference between\b",
)

_COUNT_PROFILE_PATTERNS = (
    r"\bhow many\b[^?]*\bprofiles\b",
    r"\bnumber of\b[^?]*\bprofiles\b",
    r"\bcount (?:of )?profiles\b",
    r"\bprofile counts?\b",
)

_ENUMERATE_FLOAT_PATTERNS = (
    r"\bhow many (?:argo )?(?:floats|wmo)s?\b",
    r"\bnumber of (?:available )?(?:floats|wmo)s?\b",
    r"\b(?:list|show|give|display)\b.*\b(?:all |available )?(?:floats|wmo (?:ids?|numbers?))\b",
    r"\b(?:which|what)\b.*\b(?:floats|wmo)\b.*\b(?:available|exist)\b",
)

_METADATA_PATTERNS = (
    r"\bdata modes?\b",
    r"\bqc coverage\b",
    r"\bmetadata\b",
    r"\bprovenance\b",
    r"\bwhen was\b.*\bcollected\b",
)

_LIST_ALL_PROFILE_PATTERNS = (
    r"\b(?:list|show|display)\s+(?:me\s+)?(?:all|every)\s+profiles?\b",
    r"\ball profiles\b",
    r"\blist\s+(?:the\s+)?profiles?\b",
)

_GOOD_QUALITY_PATTERNS = (
    r"\bgood[- ]quality\b",
    r"\bgood quality\b",
    r"\bacceptable (?:quality|qc)\b",
    r"\bhigh[- ]quality\b",
)

_QC_FLAG_PATTERNS = (
    r"\bqc flags?\b",
    r"\bquality flags?\b",
    r"\bqc\b",
)

_PROFILE_QC_PATTERNS = (
    r"\bprofile[- ]qc\b",
    r"\bprofile quality\b",
    r"\bqc category\b",
    r"\bprofile[- ]level qc\b",
)

_MEASUREMENT_QC_PATTERNS = (
    r"\bmeasurement[- ]qc\b",
    r"\bmeasurement[- ]level qc\b",
    r"\blevel qc\b",
    r"\bqc (?:flags?|codes?)\b",
)

_LIMIT_WORD = r"(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)"

#: Largest cycle range that is expanded into ``cycle_numbers``.
_MAX_CYCLE_RANGE_SPAN = 100

#: Words that unambiguously introduce an ARGO platform identifier.  A bare
#: "ID" is deliberately excluded: it also appears in unrelated contexts such
#: as "order ID".  "platform ID" and "float ID" still work through the
#: platform/float cue.
_WMO_CUE_WORDS = frozenset({"wmo", "float", "floats", "platform", "platforms"})

#: How far a cue may sit from the number: 3 tokens before, 2 after.
_WMO_CUE_BEFORE = 3
_WMO_CUE_AFTER = 2

#: Ends the cycle clause before an unrelated filter introducer begins.
_CYCLE_CLAUSE_STOP = re.compile(
    r"\b(?:for|with|where|from|between|at|in|during|of|on|having|sorted|order|"
    r"year|years|since|after|before|until)\b"
    r"|[.?!]"
)

_CYCLE_NUMBER = r"#?\s*\d{1,4}"
_CYCLE_LIST_TAIL = rf"(?:\s*(?:,\s*and\b|[,;]|and\b|&)\s*{_CYCLE_NUMBER}\b)*"

#: Character/word classes that make a cycle clause incomplete if they remain
#: after the one supported expression has been consumed.
_CYCLE_RESIDUE = re.compile(
    r"\d|\bcycles?\b|\brange\b|[(),;&]|\band\b|\bto\b|\bthrough\b|-"
)

#: A later ``cycle``/``cycles`` keyword is another filter instruction only when
#: its own clause opens with a cycle number.  This is what keeps sort wording
#: such as "sorted by cycle", "highest cycle first" and "order by cycle desc"
#: from being mistaken for a second filter.
_CYCLE_INSTRUCTION_HEAD = re.compile(r"^[\s(]*(?:range\s+)?#?\s*\d{1,4}\b")

#: A QC flag must be a single character that is not glued to a longer word.
#: The trailing lookahead is what stops "and cycle" from donating a 'c'.
_QC_FLAG_TOKEN = r"[0-9a-f](?![a-z0-9])"

#: Separators between QC flags, including the Oxford comma (``, and``).
_QC_FLAG_SEPARATOR = r",\s*and\b|,|and\b|&"

#: Words that make a following four-digit value an explicit calendar date.
_YEAR_CUE_WORDS = frozenset(
    {
        "in",
        "during",
        "for",
        "of",
        "since",
        "after",
        "before",
        "until",
        "to",
        "from",
        "between",
        "on",
        "year",
        "years",
        "january", "jan", "february", "feb", "march", "mar",
        "april", "apr", "may", "june", "jun", "july", "jul",
        "august", "aug", "september", "sep", "sept", "october", "oct",
        "november", "nov", "december", "dec",
    }
)

_CYCLE_COUNT_CLARIFICATION = (
    "Cycle counts depend on which cycle numbers exist in the data. Please "
    "give explicit cycle numbers (for example 'cycles 10 and 11') or a "
    "cycle range (for example 'cycles 10 to 20')."
)

_CYCLE_EXPRESSION_CLARIFICATION = (
    "The cycle expression could not be read completely. Give one list (for "
    "example 'cycles 10 and 11') or one range (for example 'cycles 10 to "
    "20'); do not combine several lists or ranges in one request."
)


# ---------------------------------------------------------------------------
# Extraction state
# ---------------------------------------------------------------------------


@dataclass
class _Extraction:
    parameters: list[Parameter] = field(default_factory=list)
    wmo_numbers: list[int] = field(default_factory=list)
    cycles: list[int] = field(default_factory=list)
    time_range: TimeRange | None = None
    bbox: BoundingBox | None = None
    radius: RadiusSearch | None = None
    pressure_range: tuple[float, float] | None = None
    depth_range_m: tuple[float, float] | None = None
    depth_requested: bool = False
    qc_filter: QcFilter | None = None
    data_mode: DataMode | None = None
    values: MeasurementValues = MeasurementValues.RAW
    values_explicit: bool = False
    aggregations: list[Aggregation] = field(default_factory=list)
    sort_field: SortField | None = None
    sort_direction: SortDirection | None = None
    limit: int | None = None
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    conversational: bool = False

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


def _normalise(text: str) -> str:
    normalised = unicodedata.normalize("NFKC", text or "")
    normalised = normalised.replace("\u00b0", " deg ")
    normalised = normalised.replace("\u2013", "-").replace("\u2014", "-")
    normalised = normalised.replace("\u2212", "-")
    normalised = re.sub(r"\s+", " ", normalised).strip().lower()
    return normalised


def _matches(text: str, patterns) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


def _extract_parameters(text: str, ex: _Extraction) -> None:
    found = {
        parameter
        for parameter, pattern in _PARAMETER_ALIASES
        if re.search(pattern, text)
    }
    ex.parameters = [parameter for parameter in Parameter if parameter in found]
    if ex.parameters:
        ex.note(
            "parameters: " + ", ".join(parameter.value for parameter in ex.parameters)
        )

    if "oxygen" in text and Parameter.DOXY not in found:
        # Defensive: the alias list above already covers this case.
        ex.ambiguities.append("The term 'oxygen' is ambiguous.")


def _extract_wmos(text: str, ex: _Extraction) -> None:
    """Extract only *cued* seven-digit identifiers.

    The window is three tokens before the number and two tokens after it, which
    covers "WMO 1900121", "float 1900121", "platform ID 1900121" and the
    second number in "WMO 1900121 and 2900226", while rejecting unrelated
    numbers such as "order 1234567 status".
    """

    tokens = re.findall(r"[a-z0-9]+", text)
    seen: list[int] = []
    ignored: list[int] = []

    for match in re.finditer(r"\b(\d{7})\b", text):
        value = int(match.group(1))
        position = len(re.findall(r"[a-z0-9]+", text[: match.start()]))
        before = tokens[max(0, position - _WMO_CUE_BEFORE) : position]
        after = tokens[position + 1 : position + 1 + _WMO_CUE_AFTER]
        if any(token in _WMO_CUE_WORDS for token in before + after):
            if value not in seen:
                seen.append(value)
        elif value not in ignored:
            ignored.append(value)

    ex.wmo_numbers = seen
    if seen:
        ex.diagnostics.append(
            "explicitly identified WMO identifier(s): "
            + ", ".join(str(value) for value in seen)
        )
    if ignored:
        ex.diagnostics.append(
            "seven-digit number(s) ignored (no WMO/float/platform/ID cue): "
            + ", ".join(str(value) for value in ignored)
        )
        if re.search(r"\b(?:profiles?|data|float|compare)\b", text):
            ex.ambiguities.append(
                "A seven-digit number appears without an identifier cue. Say "
                "'WMO <number>' or 'float <number>' so it is not mistaken for "
                "an unrelated value."
            )


def _has_additional_cycle_instruction(text: str, start: int) -> bool:
    """Return True when another cycle *filter* starts at/after ``start``.

    Every later cycle keyword is judged by its own clause: only a clause that
    begins with an optional parenthesis/``range`` and a cycle number carries
    an instruction.  A bare reference used for ordering (``sorted by cycle``,
    ``highest cycle first``) has no number and is not a second filter.
    """

    for keyword in re.finditer(r"\bcycles?\b", text[start:]):
        after = text[start + keyword.end() :]
        stop = _CYCLE_CLAUSE_STOP.search(after)
        clause = after[: stop.start()] if stop else after
        if _CYCLE_INSTRUCTION_HEAD.match(clause):
            return True
    return False


#: Numeric expressions that legitimately own their numbers.  Everything a
#: cycle clause does not contain is checked against these spans: a value
#: outside every span is an unexplained continuation and clarifies.  The
#: spans come from the same grammar the extractors use, so protection ends
#: where the expression ends instead of leaking to a neighbouring number.
_NUMERIC_FILTER_PATTERNS = (
    # pressure range, with or without unit
    r"(?:\bpressure\s*)?(?:\bbetween\s+|\bfrom\s+)?-?\d+(?:\.\d+)?\s*(?:-|to|and)\s*-?\d+(?:\.\d+)?\s*(?:dbar|decibar|db)\b",
    r"\bpressure\s+(?:between\s+|from\s+)?-?\d+(?:\.\d+)?\s*(?:-|to|and)\s*-?\d+(?:\.\d+)?",
    # single pressure value
    r"\b(?:at|pressure of|around)\s*-?\d+(?:\.\d+)?\s*(?:dbar|decibar|db)\b",
    r"\bpressure(?:\s+(?:is|of|around|at)\b|\s*=)?\s*-?\d+(?:\.\d+)?\s*(?:dbar|decibar|db)\b",
    # depth range and single depth value
    r"\bdepth\s*(?:between|from)?\s*\d+(?:\.\d+)?\s*(?:-|to|and)\s*\d+(?:\.\d+)?\s*(?:m|metres?|meters?)?",
    r"\b(?:deeper than|below|at depth(?: of)?|at)\s*\d+(?:\.\d+)?\s*(?:m|metres?|meters?)\b",
    # latitude/longitude ranges and single coordinates
    r"\blat(?:itude)?\s*(?:between|from)?\s*-?\d{1,3}(?:\.\d+)?\s*(?:deg\s*)?(?:and|to|-)\s*-?\d{1,3}(?:\.\d+)?(?!\d)",
    r"\blon(?:gitude)?\s*(?:between|from)?\s*-?\d{1,3}(?:\.\d+)?\s*(?:deg\s*)?(?:and|to|-)\s*-?\d{1,3}(?:\.\d+)?(?!\d)",
    r"\blat(?:itude)?\s*(?:of\s*)?-?\d+(?:\.\d+)?",
    r"\blon(?:gitude)?\s*(?:of\s*)?-?\d+(?:\.\d+)?",
    # radius
    r"\bwithin\s+\d+(?:\.\d+)?\s*(?:km|kilometres?|kilometers?)\b",
    # QC flag list: single-character flags only, Oxford comma included
    r"\b(?:qc|quality)\s*flags?\s*[0-9a-f](?:\s*(?:,\s*and\b|,|and\b|&)\s*[0-9a-f])*",
)

#: Date expressions in the same order ``_extract_dates`` tries them.  Only the
#: first matching expression is protected, so a second range or an extra year
#: is not absorbed by an earlier date filter.
_DATE_SPAN_PATTERNS = (
    r"\b(?:from|between)\s+\d{4}-\d{2}-\d{2}\s+(?:to|and|until)\s+\d{4}-\d{2}-\d{2}\b",
    r"\b(?:on)\s+\d{4}-\d{2}-\d{2}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b(?:from|between)\s+(?:jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\s+(?:and|to)\s+(?:jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\s+\d{4}\b",
    r"\b(?:from|between)\s+\d{4}\s+(?:to|and|until)\s+\d{4}\b",
    r"\b(?:since|after)\s+\d{4}\b",
    r"\b(?:before|until|up to)\s+\d{4}\b",
    r"\b(?:in|during|for|of)\s+\d{4}\b",
    r"\b(?:19|20)\d{2}\b",
)


def _protected_numeric_spans(text: str) -> list[tuple[int, int]]:
    """Spans of numbers owned by a recognised filter expression."""

    spans: list[tuple[int, int]] = []
    for pattern in _NUMERIC_FILTER_PATTERNS:
        for match in re.finditer(pattern, text):
            spans.append((match.start(), match.end()))

    for pattern in _DATE_SPAN_PATTERNS:
        match = re.search(pattern, text)
        if match:
            spans.append((match.start(), match.end()))
            break

    return spans


def _has_orphan_cycle_number(suffix: str) -> bool:
    """Detect an unexplained number anywhere after the cycle clause."""

    tail = suffix.strip()
    if not tail:
        return False

    spans = _protected_numeric_spans(tail)
    for match in re.finditer(r"\d+(?:\.\d+)?", tail):
        digits = match.group(0).split(".")[0]
        if len(digits) >= 5:
            # WMO/platform identifiers are outside the cycle value domain.
            continue
        if any(
            start <= match.start() and match.end() <= end
            for start, end in spans
        ):
            continue
        return True
    return False


def _scan_cycle_expression(text: str):
    """Scan exactly one cycle expression inside its own clause.

    Returns ``(kind, values, malformed, leftover)`` where ``kind`` is
    ``"range"``, ``"list"`` or ``None``.  The clause runs from the keyword to
    the next unrelated-filter introducer (``for``/``with``/``from``/...) or
    sentence end.  After consuming one complete expression, any residual
    cycle-like token is reported as ``leftover`` so a valid prefix can never
    become an executable partial filter.  The text beyond the clause boundary
    is checked too, so a second instruction after an unrelated filter
    introducer is not lost.
    """

    keyword = re.search(r"\bcycles?\b", text)
    if keyword is None:
        return None, (), False, False

    rest = text[keyword.end() :]
    stop = _CYCLE_CLAUSE_STOP.search(rest)
    clause = (rest[: stop.start()] if stop else rest).strip()
    clause = re.sub(r"^range\b", "", clause).strip()

    # "highest cycle" / "lowest cycle" are sort wording, not a filter.
    if not re.search(r"\d", clause):
        return None, (), False, False

    opened = False
    if clause.startswith("("):
        opened = True
        clause = clause[1:].lstrip()

    range_match = re.match(
        rf"({_CYCLE_NUMBER})\s*(?:to|through|-)\s*({_CYCLE_NUMBER})\b", clause
    )
    list_match = re.match(rf"({_CYCLE_NUMBER})\b{_CYCLE_LIST_TAIL}", clause)

    if range_match:
        kind = "range"
        matched = range_match.group(0)
        remainder = clause[range_match.end() :]
    elif list_match:
        kind = "list"
        matched = list_match.group(0)
        remainder = clause[list_match.end() :]
    else:
        # A cycle clause with numbers that fits neither shape is malformed.
        return None, (), True, False

    values = tuple(int(value) for value in re.findall(r"\d{1,4}", matched))

    remainder = remainder.strip()
    if opened:
        if not remainder.startswith(")"):
            return None, (), True, False
        remainder = remainder[1:].strip()
        if remainder.startswith(")"):
            return None, (), True, False

    if _CYCLE_RESIDUE.search(remainder):
        return None, (), False, True

    # A second cycle instruction may sit beyond the clause boundary
    # ("with cycle 25", "for WMO 1900121 and cycle 25"): the first clause is
    # clean, so the whole rest of the question must be checked as well.
    if _has_additional_cycle_instruction(text, keyword.end()):
        return None, (), False, True

    # An orphan trailing number ("... for WMO 1900121 and 25") may be another
    # cycle instruction.  It is only accepted when its own context explains it
    # as a supported numeric filter.
    suffix = rest[stop.start() :] if stop else ""
    if _has_orphan_cycle_number(suffix):
        return None, (), False, True

    return kind, values, False, False


def _extract_cycles(text: str, ex: _Extraction) -> None:
    # Every cycle-count phrase depends on which cycle numbers exist in the
    # data, so none of them may become a guessed range or a result limit.
    count_match = re.search(
        rf"\b(first|latest|last|newest|oldest|earliest)\s+({_LIMIT_WORD})\s+cycles?\b",
        text,
    )

    explicit_anywhere = re.search(r"\bcycles?\s+\d{1,4}\b", text)

    if count_match:
        ex.note(f"cycle count requested: {count_match.group(0)}")
        if explicit_anywhere:
            ex.ambiguities.append(
                "The question gives both explicit cycle numbers and a cycle "
                "count; please keep only one of them."
            )
        else:
            ex.ambiguities.append(_CYCLE_COUNT_CLARIFICATION)
        return

    kind, values, malformed, leftover = _scan_cycle_expression(text)

    if malformed or leftover:
        ex.ambiguities.append(_CYCLE_EXPRESSION_CLARIFICATION)
        return

    if not values:
        return

    if kind == "range":
        start, end = values
        if end < start:
            ex.errors.append(
                "Cycle range end cannot be earlier than its start."
            )
            return
        if end - start + 1 > _MAX_CYCLE_RANGE_SPAN:
            ex.errors.append(
                f"Cycle ranges are limited to {_MAX_CYCLE_RANGE_SPAN} cycles."
            )
            return
        ex.cycles = list(range(start, end + 1))
        ex.note(f"cycle range: {start}-{end}")
        return

    for value in values:
        if value not in ex.cycles:
            ex.cycles.append(value)
    ex.note("cycle numbers: " + ", ".join(str(value) for value in ex.cycles))


def _extract_dates(text: str, ex: _Extraction) -> None:
    iso = r"(\d{4})-(\d{2})-(\d{2})"

    if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text):
        ex.ambiguities.append(
            "The numeric date format is ambiguous; use YYYY-MM-DD or a year."
        )
        return

    match = re.search(
        rf"\b(?:from|between)\s+{iso}\s+(?:to|and|until)\s+{iso}\b", text
    )
    if match:
        start = _make_date(match.group(1), match.group(2), match.group(3), ex)
        end = _make_date(match.group(4), match.group(5), match.group(6), ex)
        if start and end:
            # Inclusive end date: the whole final day is part of the range.
            ex.time_range = TimeRange(
                start=start, end=datetime.combine(end.date(), time.max)
            )
            ex.note(
                f"time range: {start.date().isoformat()} to {end.date().isoformat()}"
            )
        return

    match = re.search(rf"\b(?:on)\s+{iso}\b", text) or re.search(rf"\b{iso}\b", text)
    if match:
        start = _make_date(match.group(1), match.group(2), match.group(3), ex)
        if start:
            ex.time_range = TimeRange(
                start=start, end=datetime.combine(start.date(), datetime.max.time())
            )
            ex.note(f"time range: {start.date().isoformat()}")
        return

    match = re.search(
        r"\b(?:from|between)\s+(jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|"
        r"aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\s+(?:and|to)\s+"
        r"(jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|"
        r"nov\w*|dec\w*)\s+(\d{4})\b",
        text,
    )
    if match:
        year = int(match.group(3))
        start_month = _MONTHS.get(match.group(1)[:3], _MONTHS.get(match.group(1)))
        end_month = _MONTHS.get(match.group(2)[:3], _MONTHS.get(match.group(2)))
        if start_month and end_month:
            start = datetime(year, start_month, 1)
            last_day = (datetime(year, end_month + 1, 1) - timedelta(days=1)).date()
            end = datetime.combine(last_day, time.max)
            ex.time_range = TimeRange(start=start, end=end)
            ex.note(f"time range: {start.date()} to {end.date()}")
            return

    match = re.search(r"\b(?:from|between)\s+(\d{4})\s+(?:to|and|until)\s+(\d{4})\b", text)
    if match:
        start_year, end_year = int(match.group(1)), int(match.group(2))
        ex.time_range = TimeRange(
            start=datetime(start_year, 1, 1),
            end=datetime.combine(date(end_year, 12, 31), time.max),
        )
        ex.note(f"time range: {start_year} to {end_year}")
        return

    match = re.search(r"\b(?:since|after)\s+(\d{4})\b", text)
    if match:
        year = int(match.group(1))
        ex.time_range = TimeRange(start=datetime(year, 1, 1))
        ex.note(f"time range: since {year}")
        return

    match = re.search(r"\b(?:before|until|up to)\s+(\d{4})\b", text)
    if match:
        year = int(match.group(1))
        end = (
            datetime.combine(date(year - 1, 12, 31), time.max)
            if text[match.start() :].startswith(("before", "up to"))
            else datetime.combine(date(year, 12, 31), time.max)
        )
        ex.time_range = TimeRange(end=end)
        ex.note(f"time range: until {year}")
        return

    match = re.search(r"\b(?:in|during|for|of)\s+(\d{4})\b", text)
    if match:
        year = int(match.group(1))
        ex.time_range = TimeRange(
            start=datetime(year, 1, 1),
            end=datetime.combine(date(year, 12, 31), time.max),
        )
        ex.note(f"time range: {year}")
        return

    # A four-digit value with no date cue ("... and 2025") could be a year or
    # a cycle number.  Both readings are legal, so the parser never guesses.
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    if match:
        if not _has_date_cue_before(text, match.start()):
            ex.ambiguities.append(
                f"The number {match.group(1)} could be a calendar year or a "
                f"cycle number. Say 'in {match.group(1)}' for the year, or "
                f"'cycle {match.group(1)}' for the cycle."
            )
            return

        year = int(match.group(1))
        ex.time_range = TimeRange(
            start=datetime(year, 1, 1),
            end=datetime.combine(date(year, 12, 31), time.max),
        )
        ex.note(f"time range: {year}")
        return


def _has_date_cue_before(text: str, start: int) -> bool:
    """True when the word before a bare year marks it as an explicit date."""

    prefix = text[:start].rstrip()
    match = re.search(r"([a-z]+)\s*$", prefix)
    return bool(match) and match.group(1) in _YEAR_CUE_WORDS



def _make_date(year: str, month: str, day: str, ex: _Extraction):
    try:
        return datetime(int(year), int(month), int(day))
    except ValueError:
        ex.errors.append(f"Invalid calendar date: {year}-{month}-{day}.")
        return None


def _extract_geography(text: str, ex: _Extraction) -> None:
    # Coordinate magnitudes are at most three digits before the decimal, so a
    # trailing four-digit year ("longitude 75 and 2025") is not read as a
    # coordinate range endpoint.
    lat_range_pattern = (
        r"\blat(?:itude)?\s*(?:between|from)?\s*(-?\d{1,3}(?:\.\d+)?)\s*"
        r"(?:deg\s*)?(?:and|to|-)\s*(-?\d{1,3}(?:\.\d+)?)(?!\d)"
    )
    lon_range_pattern = (
        r"\blon(?:gitude)?\s*(?:between|from)?\s*(-?\d{1,3}(?:\.\d+)?)\s*"
        r"(?:deg\s*)?(?:and|to|-)\s*(-?\d{1,3}(?:\.\d+)?)(?!\d)"
    )
    lat_ranges = list(re.finditer(lat_range_pattern, text))
    lon_ranges = list(re.finditer(lon_range_pattern, text))
    range_spans = [match.span() for match in lat_ranges + lon_ranges]

    def _outside_ranges(match) -> bool:
        return not any(start <= match.start() < end for start, end in range_spans)

    lat_singles = [
        match
        for match in re.finditer(
            r"\blat(?:itude)?\s*(?:of\s*)?(-?\d+(?:\.\d+)?)\b", text
        )
        if _outside_ranges(match)
    ]
    lon_singles = [
        match
        for match in re.finditer(
            r"\blon(?:gitude)?\s*(?:of\s*)?(-?\d+(?:\.\d+)?)\b", text
        )
        if _outside_ranges(match)
    ]

    # Every extracted value is validated, including extras that follow an
    # otherwise valid bounding box.
    lat_values = [float(match.group(1)) for match in lat_ranges]
    lat_values.extend(float(match.group(2)) for match in lat_ranges)
    lat_values.extend(float(match.group(1)) for match in lat_singles)
    lon_values = [float(match.group(1)) for match in lon_ranges]
    lon_values.extend(float(match.group(2)) for match in lon_ranges)
    lon_values.extend(float(match.group(1)) for match in lon_singles)

    invalid = False
    if any(not -90 <= value <= 90 for value in lat_values):
        ex.errors.append("Latitude must lie between -90 and 90 degrees.")
        invalid = True
    if any(not -180 <= value <= 180 for value in lon_values):
        ex.errors.append("Longitude must lie between -180 and 180 degrees.")
        invalid = True

    if lat_ranges and lon_ranges:
        lat_boxes = {
            (float(match.group(1)), float(match.group(2)))
            for match in lat_ranges
        }
        lon_boxes = {
            (float(match.group(1)), float(match.group(2)))
            for match in lon_ranges
        }
        if (
            len(lat_boxes) == 1
            and len(lon_boxes) == 1
            and not lat_singles
            and not lon_singles
        ):
            (min_latitude, max_latitude) = next(iter(lat_boxes))
            (min_longitude, max_longitude) = next(iter(lon_boxes))
            ex.bbox = BoundingBox(
                min_latitude=min_latitude,
                max_latitude=max_latitude,
                min_longitude=min_longitude,
                max_longitude=max_longitude,
            )
            ex.note("explicit latitude/longitude bounding box")
        else:
            ex.ambiguities.append(
                "Only one latitude range and one longitude range can be applied; "
                "give a single bounding box without extra coordinates."
            )
    elif lat_ranges or lon_ranges:
        ex.ambiguities.append(
            "Both a latitude range and a longitude range are required for a "
            "geographic filter."
        )
    elif lat_singles or lon_singles:
        if not invalid:
            if lat_singles and lon_singles:
                ex.ambiguities.append(
                    "A single latitude/longitude point is not supported; "
                    "give a coordinate range (for example 'latitude 10 and "
                    "20 longitude 70 and 80')."
                )
            else:
                ex.ambiguities.append(
                    "Both a latitude and a longitude are required. A single "
                    "coordinate is not supported; give a coordinate range "
                    "(for example 'latitude 10 and 20 longitude 70 and 80')."
                )

    near = re.search(
        r"(?:\bnear\b|\baround\b|\bclose to\b|\bwithin\s+\d+(?:\.\d+)?\s*"
        r"(?:km|kilometres?|kilometers?)\s+of\b)\s*"
        r"(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?([ns])?\s*,?\s*"
        r"(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?([ew])?",
        text,
    )
    if near:
        latitude = float(near.group(1))
        longitude = float(near.group(3))
        if near.group(2) == "s":
            latitude = -latitude
        if near.group(4) == "w":
            longitude = -longitude

        radius = re.search(
            r"\bwithin\s+(\d+(?:\.\d+)?)\s*(?:km|kilometres?|kilometers?)\b", text
        )
        if radius:
            ex.radius = RadiusSearch(
                latitude=latitude,
                longitude=longitude,
                radius_km=float(radius.group(1)),
            )
            ex.note("explicit radius search (unsupported in Phase 3C)")
        else:
            ex.radius = RadiusSearch(latitude=latitude, longitude=longitude)
            ex.ambiguities.append(
                "A radius is required for a 'near' search; please supply the "
                "distance in kilometres (for example 'within 100 km')."
            )

    for region in _REGION_TERMS:
        if region in text:
            ex.ambiguities.append(
                f"'{region}' is a named ocean region, which has no stored "
                "boundary. Please give an explicit latitude/longitude box."
            )
            ex.note(f"named region requested: {region}")
            break


def _extract_pressure_and_depth(text: str, ex: _Extraction) -> None:
    pair_pattern = (
        r"\b(?:pressure\s*)?(?:between\s+|from\s+)?(-?\d+(?:\.\d+)?)\s*"
        r"(?:-|to|and)\s*(-?\d+(?:\.\d+)?)\s*(?:dbar|decibar|db)\b"
    )
    pair_matches = list(re.finditer(pair_pattern, text))
    pair_spans = [match.span() for match in pair_matches]
    pair_values = [
        (float(match.group(1)), float(match.group(2)))
        for match in pair_matches
    ]

    # Extra unit-qualified values attached to the same expression, such as
    # "pressure 0 to 500 dbar and 700 dbar".
    continuation_values: list[float] = []
    continuation_start = max((end for _, end in pair_spans), default=0)
    for match in re.finditer(
        r"(?:,|\band\b)\s*(-?\d+(?:\.\d+)?)\s*(?:dbar|decibar|db)\b",
        text[continuation_start:],
    ):
        continuation_values.append(float(match.group(1)))

    incomplete_range = (
        # A range word with no numeric bound immediately after it, even when
        # another filter clause follows, e.g. "pressure between cycles 10 to
        # 20", "pressure from WMO 1900121", "pressure to in 2021".  The
        # lookahead only inspects the text directly after the range word, so a
        # number in a following clause is never mistaken for a bound.
        re.search(
            r"\bpressure\s+(?:between|from)\b(?!\s*-?\d)", text
        )
        or re.search(r"\bpressure\s+to\b(?!\s*-?\d)", text)
        # A range was started but neither numeric bound was supplied, e.g.
        # "pressure between and", "pressure from and", "pressure between to".
        or re.search(
            r"\bpressure\s+(?:between|from)\s*(?:-|to|and)\b", text
        )
        # A dangling connector without the leading range word, e.g.
        # "pressure to and" or "pressure and to".
        or re.search(
            r"\bpressure\s+(?:to|and)\s+(?:to|and)\b", text
        )
        # A lone trailing connector, e.g. "pressure to" / "pressure and."
        or re.search(
            r"\bpressure\s+(?:to|and)\b\s*[.!?]*$", text
        )
        or re.search(
            r"\bpressure\s+(?:between|from)\s*-?\d+(?:\.\d+)?\s*"
            r"(?:-|to|and)\s*(?!\s*-?\d)",
            text,
        )
        or re.search(
            r"\bpressure\s+(?:between|from)\s*(?:-|to|and)\s*"
            r"-?\d+(?:\.\d+)?",
            text,
        )
        or re.search(r"\bpressure\s+(?:between|from)\s*$", text)
        or re.search(
            r"\bpressure\s+-?\d+(?:\.\d+)?\s*(?:-|to|and)\s*"
            r"(?!\s*-?\d)",
            text,
        )
        or re.search(
            r"\bpressure\s+(?:to|and)\b\s*-?\d+(?:\.\d+)?", text
        )
    )
    # Unitless pressure ranges are recognised by the numeric-span guard but
    # cannot be converted to dbar, so they clarify instead of disappearing.
    unitless_range = re.search(
        r"\bpressure\s+(?:between\s+|from\s+)?-?\d+(?:\.\d+)?\s*"
        r"(?:-|to|and)\s*-?\d+(?:\.\d+)?\b(?!\s*(?:dbar|decibar|db)\b)",
        text,
    )

    single_matches = []
    for pattern in (
        r"\bpressure(?:\s+(?:is|of|around|at)\b|\s*=)?\s*"
        r"(-?\d+(?:\.\d+)?)\s*(dbar|decibar|db)?\b",
        r"\b(?:at|around)\s*(-?\d+(?:\.\d+)?)\s*(dbar|decibar|db)\b",
    ):
        for match in re.finditer(pattern, text):
            if any(
                start <= match.start() < end for start, end in pair_spans
            ):
                continue
            single_matches.append(match)

    if pair_matches:
        unique_pairs = set(pair_values)
        extra_singles = [
            float(match.group(1)) for match in single_matches
        ]
        if len(unique_pairs) > 1 or extra_singles or continuation_values:
            ex.ambiguities.append(
                "More than one pressure expression was supplied; give a single "
                "pressure range or value."
            )
        else:
            ex.pressure_range = pair_values[0]
            if len(pair_matches) > 1:
                ex.note("pressure range in dbar (repeated identically)")
            else:
                ex.note("pressure range in dbar")
    elif incomplete_range:
        ex.ambiguities.append(
            "The pressure range is incomplete; give both bounds (for example "
            "'pressure 0 to 500 dbar')."
        )
    elif unitless_range:
        ex.ambiguities.append(
            "A pressure range needs a unit. Say 'pressure 0 to 500 dbar'; "
            "without a unit the filter is not applied."
        )
    elif single_matches:
        values = [float(match.group(1)) for match in single_matches]
        values.extend(continuation_values)
        if any(match.group(2) is None for match in single_matches):
            ex.ambiguities.append(
                "A pressure value needs a unit. Say 'pressure 500 dbar'; "
                "without a unit the filter is not applied."
            )
        elif len(set(values)) > 1:
            ex.ambiguities.append(
                "More than one pressure value was supplied; give a single "
                "pressure expression."
            )
        else:
            value = values[0]
            ex.pressure_range = (value, value)
            ex.note("single pressure value in dbar")

    depth_pair = re.search(
        r"\bdepth\s*(?:between|from)?\s*(\d+(?:\.\d+)?)\s*(?:-|to|and)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:m|metres?|meters?)?\b",
        text,
    )
    depth_value = re.search(
        r"\b(?:deeper than|below|at depth(?: of)?|at)\s*(\d+(?:\.\d+)?)\s*"
        r"(?:m|metres?|meters?)\b",
        text,
    )
    if depth_pair:
        ex.depth_range_m = (
            float(depth_pair.group(1)),
            float(depth_pair.group(2)),
        )
        ex.depth_requested = True
        ex.note("depth range in metres (never converted to pressure)")
    elif depth_value:
        value = float(depth_value.group(1))
        ex.depth_range_m = (value, value)
        ex.depth_requested = True
        ex.note("depth value in metres (never converted to pressure)")

    if ex.pressure_range is not None and ex.depth_range_m is not None:
        ex.note("both depth and pressure supplied; QuerySpec will ask which to use")


def _parse_flag_list(raw: str) -> list[str]:
    parts = re.split(r"\s*(?:,|and|&)\s*", raw)
    return [
        part
        for part in (candidate.strip() for candidate in parts)
        if len(part) == 1 and part.isalnum()
    ]


def _extract_qc(text: str, ex: _Extraction) -> None:
    good_quality = _matches(text, _GOOD_QUALITY_PATTERNS)

    # 1. Explicit profile category ("profile QC category A").
    category = re.search(r"\b(?:profile\s*qc|qc\s*category)\s*([a-f])\b", text)
    if category:
        ex.qc_filter = QcFilter(
            scope=QcScope.PROFILE, flags=frozenset({category.group(1).upper()})
        )
        ex.note("profile-level QC category")
        return

    # 2. Explicit flag lists.  Each flag must be a standalone single character,
    #    so "and cycle" can never contribute the 'c'.  Digits are measurement
    #    flags, letters are profile categories; mixing them is invalid.
    flags_match = re.search(
        r"\b(?:qc|quality)\s*flags?\s*("
        + _QC_FLAG_TOKEN
        + r"(?:\s*(?:"
        + _QC_FLAG_SEPARATOR
        + r")\s*"
        + _QC_FLAG_TOKEN
        + r")*)",
        text,
    )
    if flags_match:
        raw_flags = _parse_flag_list(flags_match.group(1))
        digits = [flag for flag in raw_flags if flag.isdigit()]
        letters = [flag for flag in raw_flags if flag.isalpha()]
        if digits and letters:
            ex.errors.append(
                "QC flags mix measurement-level digits and profile-level "
                "categories; use one QC scope per filter."
            )
            return
        if letters:
            ex.qc_filter = QcFilter(
                scope=QcScope.PROFILE,
                flags=frozenset(flag.upper() for flag in letters),
            )
            ex.note("profile-level QC flags")
        else:
            ex.qc_filter = QcFilter(
                scope=QcScope.MEASUREMENT, flags=frozenset(digits)
            )
            ex.note("measurement-level QC flags")
        return

    if not good_quality:
        return

    # 3. "Good quality" needs a scope word; the scope is never assumed.
    profile_scope = _matches(text, _PROFILE_QC_PATTERNS) or bool(
        re.search(r"\bprofiles?\b", text)
    )
    measurement_scope = _matches(text, _MEASUREMENT_QC_PATTERNS) or bool(
        re.search(r"\b(?:measurements?|levels?|values?)\b", text)
    )

    if profile_scope and measurement_scope:
        ex.ambiguities.append(
            "The QC scope is ambiguous: say 'profile QC' or 'measurement QC'."
        )
        return

    if profile_scope:
        ex.qc_filter = good_quality_filter(QcScope.PROFILE)
        ex.note("profile-level good-quality policy (A/B, >=75% good levels)")
    elif measurement_scope:
        ex.qc_filter = good_quality_filter(QcScope.MEASUREMENT)
        ex.note("measurement-level good-quality policy (flags 1 and 2)")
    else:
        ex.ambiguities.append(
            "The request says 'good quality' without saying whether it means "
            "whole profiles (QC categories A/B) or individual measurements "
            "(QC flags 1/2). Please specify."
        )


def _extract_data_mode(text: str, ex: _Extraction) -> None:
    if re.search(
        r"\bdelayed[- ]mode\b|\bdata[ _-]?mode\s*d\b|\bmode\s*d\b", text
    ):
        ex.data_mode = DataMode.DELAYED
        ex.note("data mode: delayed (D)")
    elif re.search(
        r"\breal[- ]time\b|\bdata[ _-]?mode\s*r\b|\bmode\s*r\b", text
    ):
        ex.data_mode = DataMode.REAL_TIME
        ex.note("data mode: real-time (R)")


def _extract_values(text: str, ex: _Extraction) -> None:
    if re.search(r"\badjusted\b", text):
        ex.values = MeasurementValues.ADJUSTED
        ex.values_explicit = True
        ex.note("adjusted measurements requested")
    elif re.search(r"\braw\b|\bunadjusted\b", text):
        ex.values = MeasurementValues.RAW
        ex.values_explicit = True
        ex.note("raw measurements requested")


def _extract_aggregations(text: str, ex: _Extraction) -> None:
    for word in _UNSUPPORTED_AGGREGATIONS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            ex.unsupported.append(
                f"'{word}' aggregation is not supported; supported operations "
                "are COUNT, MEAN, MIN and MAX."
            )
            ex.note(f"unsupported aggregation requested: {word}")

    found = {
        aggregation
        for aggregation, pattern in _AGGREGATION_ALIASES
        if re.search(pattern, text)
    }
    found_ordered = [aggregation for aggregation in Aggregation if aggregation in found]
    if found_ordered:
        ex.aggregations = found_ordered
        ex.note(
            "aggregations: " + ", ".join(aggregation.value for aggregation in found)
        )


def _extract_sort_and_limit(text: str, ex: _Extraction) -> None:
    word = _LIMIT_WORD
    limit_match = re.search(
        rf"\b(?:first|latest|newest|last|top|oldest|earliest|most recent)\s+"
        rf"({word})\s+(?:profiles?|records?|results?)\b",
        text,
    )
    if limit_match:
        raw = limit_match.group(1)
        ex.limit = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        ex.note(f"result limit: {ex.limit}")
    elif re.search(r"\b(?:latest|newest|most recent|oldest|earliest)\s+profile\b", text):
        ex.limit = 1
        ex.note("result limit: 1 (single latest/earliest profile)")

    if re.search(r"\bhighest cycle\b|\blargest cycle\b|\bmost recent cycle\b", text):
        ex.sort_field = SortField.CYCLE_NUMBER
        ex.sort_direction = SortDirection.DESC
    elif re.search(r"\blowest cycle\b|\bsmallest cycle\b|\bfirst cycle\b", text):
        ex.sort_field = SortField.CYCLE_NUMBER
        ex.sort_direction = SortDirection.ASC
    elif re.search(r"\blatest\b|\bnewest\b|\bmost recent\b", text):
        ex.sort_field = SortField.PROFILE_TIME
        ex.sort_direction = SortDirection.DESC
    elif re.search(r"\boldest\b|\bearliest\b|\bchronological\b", text):
        ex.sort_field = SortField.PROFILE_TIME
        ex.sort_direction = SortDirection.ASC
    elif re.search(r"\bfirst\b", text):
        ex.sort_field = SortField.PROFILE_TIME
        ex.sort_direction = SortDirection.ASC

    if limit_match and ex.sort_direction is None:
        prefix = limit_match.group(0).split()[0]
        ex.sort_direction = (
            SortDirection.DESC
            if prefix in {"latest", "newest", "last", "top", "most"}
            else SortDirection.ASC
        )
        ex.sort_field = SortField.PROFILE_TIME




# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_INTENT_RULES = """
Precedence (first match wins, each rule requires explicit evidence):

 1. CONVERSATIONAL       exact greeting/thanks phrase, no data words
 2. AGGREGATE (unsupported operation) explicit median/percentile/etc.
 3. COMPARE              compare/versus/difference language
 4. PARAMETER_AVAILABILITY  "which floats have <parameter>", "profiles containing"
 5. COUNT_PROFILES       "how many profiles", "number of profiles", "profile count"
 6. ENUMERATE_FLOATS     "how many floats", "list all floats/WMO ids"
 7. AGGREGATE            COUNT/MEAN/MIN/MAX wording
 8. METADATA_INFO        data-mode / QC-coverage / provenance questions
 9. PROFILE_DETAIL       a WMO identifier without an explicit list-all request
10. LIST_PROFILES        parameters or filters (date, area, QC, depth, ...)
11. SEMANTIC_DISCOVERY   no structured signal at all
"""


def _is_compare(text: str) -> bool:
    return _matches(text, _COMPARE_PATTERNS)


def _detect_intent(text: str, ex: _Extraction) -> Intent:
    if ex.conversational:
        return Intent.CONVERSATIONAL

    if ex.unsupported and ex.aggregations == [] and _is_aggregation_context(text):
        return Intent.AGGREGATE

    if _is_compare(text):
        return Intent.COMPARE

    if (
        ex.parameters
        and not re.search(r"\bqc\b|\bquality\b", text)
        and (
            re.search(
                r"\b(?:which|what)\s+(?:floats?|profiles?)\s+(?:have|contain|include)",
                text,
            )
            or re.search(r"\b(?:floats?|profiles?)\s+(?:with|containing)\b", text)
        )
    ):
        return Intent.PARAMETER_AVAILABILITY

    if _matches(text, _COUNT_PROFILE_PATTERNS):
        return Intent.COUNT_PROFILES

    if _matches(text, _ENUMERATE_FLOAT_PATTERNS):
        return Intent.ENUMERATE_FLOATS

    if ex.aggregations:
        return Intent.AGGREGATE

    if _matches(text, _METADATA_PATTERNS) and not (
        ex.parameters
        or ex.time_range
        or ex.bbox
        or ex.radius
        or ex.pressure_range
        or ex.depth_requested
        or ex.qc_filter
        or ex.data_mode
    ):
        return Intent.METADATA_INFO

    if _matches(text, _LIST_ALL_PROFILE_PATTERNS):
        return Intent.LIST_PROFILES

    if ex.wmo_numbers:
        if re.search(
            r"\b(?:first|latest|oldest|newest|last|earliest)\s+\w+\s+profiles\b",
            text,
        ):
            return Intent.LIST_PROFILES
        # Several filters turn the request into a list even when a WMO is
        # present; a bare parameter + WMO remains a detail request.
        if (
            ex.time_range
            or ex.bbox
            or ex.radius
            or ex.qc_filter
            or ex.data_mode
            or ex.pressure_range
            or ex.depth_requested
        ):
            return Intent.LIST_PROFILES
        return Intent.PROFILE_DETAIL

    if (
        ex.parameters
        or ex.time_range
        or ex.bbox
        or ex.radius
        or ex.pressure_range
        or ex.depth_requested
        or ex.qc_filter
        or ex.data_mode
        or ex.values_explicit
        or ex.cycles
        or ex.limit is not None
        or ex.sort_field is not None
        or ex.ambiguities
    ):
        return Intent.LIST_PROFILES

    return Intent.SEMANTIC_DISCOVERY


def _is_aggregation_context(text: str) -> bool:
    return bool(re.search(r"\b(?:temperature|salinity|oxygen|chlorophyll|nitrate|ph)\b", text))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _clarify(text: str, messages, diagnostics) -> ParsedQuery:
    return ParsedQuery(
        text=text,
        intent=Intent.CLARIFY,
        spec=None,
        validation=ValidationResult(
            status=ValidationStatus.NEEDS_CLARIFICATION,
            reason=" ".join(messages),
        ),
        confidence=0.5,
        diagnostics=tuple(diagnostics),
        ambiguous_terms=tuple(messages),
    )


def _unsupported(text: str, intent: Intent, messages, diagnostics) -> ParsedQuery:
    return ParsedQuery(
        text=text,
        intent=intent,
        spec=None,
        validation=ValidationResult(
            status=ValidationStatus.UNSUPPORTED, reason=" ".join(messages)
        ),
        confidence=0.8,
        diagnostics=tuple(diagnostics),
        ambiguous_terms=tuple(messages),
    )


def parse_query(user_text: str) -> ParsedQuery:
    """Parse one user question deterministically.

    Returns a :class:`ParsedQuery` whether or not the text is understood; it
    never raises for ordinary text and never touches a database.
    """

    text = _normalise(user_text)
    ex = _Extraction()

    if not text:
        return ParsedQuery(
            text=text,
            intent=None,
            spec=None,
            validation=ValidationResult(
                status=ValidationStatus.INVALID,
                reason="The question is empty.",
            ),
            confidence=0.0,
            diagnostics=("empty input",),
        )

    stripped = re.sub(r"[!?.]+$", "", text)
    if stripped in CONVERSATIONAL_PHRASES:
        ex.conversational = True
        ex.note("ordinary conversation")

    _extract_parameters(text, ex)
    _extract_wmos(text, ex)
    _extract_cycles(text, ex)
    _extract_dates(text, ex)
    _extract_geography(text, ex)
    _extract_pressure_and_depth(text, ex)
    _extract_qc(text, ex)
    _extract_data_mode(text, ex)
    _extract_values(text, ex)
    _extract_aggregations(text, ex)
    _extract_sort_and_limit(text, ex)

    intent = _detect_intent(text, ex)

    # -- parser-level failures -------------------------------------------
    if ex.errors:
        if intent is Intent.SEMANTIC_DISCOVERY and re.search(
            r"\b(?:profiles?|cycles?|wmo|data)\b", text
        ):
            intent = Intent.LIST_PROFILES
        return ParsedQuery(
            text=text,
            intent=intent,
            spec=None,
            validation=ValidationResult(
                status=ValidationStatus.INVALID, reason=" ".join(ex.errors)
            ),
            confidence=0.9,
            diagnostics=tuple(ex.diagnostics),
        )

    if ex.unsupported and intent is Intent.AGGREGATE:
        return _unsupported(text, intent, ex.unsupported, ex.diagnostics)

    if ex.ambiguities:
        return _clarify(text, ex.ambiguities, ex.diagnostics)

    # -- build and validate ----------------------------------------------
    sort = SortSpec(
        field=ex.sort_field or SortField.PROFILE_TIME,
        direction=ex.sort_direction or SortDirection.DESC,
    )

    spec = QuerySpec(
        intent=intent,
        parameters=tuple(ex.parameters),
        wmo_numbers=tuple(ex.wmo_numbers),
        cycle_numbers=tuple(ex.cycles),
        time_range=ex.time_range,
        bounding_box=ex.bbox,
        pressure_range=ex.pressure_range,
        depth_range_m=ex.depth_range_m,
        depth_requested=ex.depth_requested,
        radius_search=ex.radius,
        qc_filter=ex.qc_filter,
        data_mode=ex.data_mode,
        values=ex.values,
        aggregations=tuple(ex.aggregations),
        sort=sort,
        limit=ex.limit,
        semantic_query=text if intent is Intent.SEMANTIC_DISCOVERY else None,
        notes=tuple(ex.notes),
    )

    validation = validate_spec(spec)

    confidence = {
        Intent.CONVERSATIONAL: 0.9,
        Intent.SEMANTIC_DISCOVERY: 0.6,
        Intent.CLARIFY: 0.5,
    }.get(intent, 0.9)

    return ParsedQuery(
        text=text,
        intent=intent,
        # The candidate spec is kept for diagnostics even when the validator
        # rejects it; ``validation.status``/``validation.spec`` stay authoritative.
        spec=spec,
        validation=validation,
        confidence=confidence,
        diagnostics=tuple(ex.diagnostics),
        ambiguous_terms=(),
    )
