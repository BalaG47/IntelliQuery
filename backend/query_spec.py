"""Typed, validated representation of an interpreted ARGO data query.

Phase 3C-B scope: data model, enums, validation rules and constants only.

This module deliberately contains:

* no natural-language parsing,
* no database access (MySQL or ChromaDB),
* no SQL generation,
* no LLM calls,
* no conversation state.

Validation is pure and deterministic. It accepts a ``QuerySpec`` and returns a
``ValidationResult``. A caller may optionally supply a ``CapabilityProfile``
describing what a dataset actually contains; when no profile is supplied the
validator makes no claim about data availability (the application recognises a
parameter, which is not the same as the dataset containing it).

Standard library only, matching the conventions already used in
``backend/query_router.py`` (``dataclasses`` + ``enum``).

Scientific rules encoded here:

* Pressure (PRES) is measured in dbar and is never silently converted from, or
  to, depth in metres. A depth request produces NEEDS_CLARIFICATION.
* Raw measurements are the default; adjusted values are only used when the spec
  explicitly asks for them.
* Profile-level QC and measurement-level QC are separate scopes.
* ``PARAMETER_DATA_MODE`` is never inferred from profile-level ``DATA_MODE``;
  the model has no field that would allow that inference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# Supported vocabulary
# ---------------------------------------------------------------------------


class Intent(str, Enum):
    """The supported query families."""

    ENUMERATE_FLOATS = "enumerate_floats"
    COUNT_PROFILES = "count_profiles"
    LIST_PROFILES = "list_profiles"
    PROFILE_DETAIL = "profile_detail"
    AGGREGATE = "aggregate"
    COMPARE = "compare"
    PARAMETER_AVAILABILITY = "parameter_availability"
    METADATA_INFO = "metadata_info"
    CONVERSATIONAL = "conversational"
    SEMANTIC_DISCOVERY = "semantic_discovery"
    CLARIFY = "clarify"


class Parameter(str, Enum):
    """Canonical parameter identifiers, matching the ingestion vocabulary."""

    TEMP = "TEMP"
    PSAL = "PSAL"
    PRES = "PRES"
    DOXY = "DOXY"
    CHLA = "CHLA"
    BBP700 = "BBP700"
    NITRATE = "NITRATE"
    PH_IN_SITU_TOTAL = "PH_IN_SITU_TOTAL"
    CHLA_FLUORESCENCE = "CHLA_FLUORESCENCE"


CORE_PARAMETERS = frozenset(
    {Parameter.TEMP, Parameter.PSAL, Parameter.PRES}
)

BGC_PARAMETERS = frozenset(
    {
        Parameter.DOXY,
        Parameter.CHLA,
        Parameter.BBP700,
        Parameter.NITRATE,
        Parameter.PH_IN_SITU_TOTAL,
        Parameter.CHLA_FLUORESCENCE,
    }
)

#: Parameters the *application* understands. Presence in a dataset is a
#: separate question answered by ``CapabilityProfile`` or, later, by execution.
APPLICATION_PARAMETERS = frozenset(Parameter)


class Aggregation(str, Enum):
    """Value operations that Phase 3C-D / Phase 3 can execute."""

    COUNT = "COUNT"
    MEAN = "MEAN"
    MIN = "MIN"
    MAX = "MAX"
    MEDIAN = "MEDIAN"
    STDDEV = "STDDEV"


class ComparisonOperator(str, Enum):
    """Numeric comparison operators for Phase 3B BGC threshold queries."""

    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    EQ = "="
    BETWEEN = "between"


@dataclass(frozen=True)
class NumericFilter:
    """Validated numeric threshold or range filter on a parameter."""

    parameter: Parameter
    operator: ComparisonOperator
    value: float
    upper_value: float | None = None
    scope: str = "measurement"  # "measurement", "profile", or "float"
    aggregation: Aggregation | None = None
    unit: str | None = None


class GroupBy(str, Enum):
    """Supported grouping dimensions; no speculative values."""

    YEAR = "year"
    MONTH = "month"
    WMO = "wmo"
    PROFILE = "profile"


class SortField(str, Enum):
    """Profile metadata a result may be ordered by."""

    PROFILE_TIME = "profile_time"
    CYCLE_NUMBER = "cycle_number"
    WMO_NUMBER = "wmo_number"
    LATITUDE = "latitude"
    LONGITUDE = "longitude"
    PROFILE_ID = "profile_id"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class QcScope(str, Enum):
    """QC scopes are intentionally not interchangeable."""

    PROFILE = "profile"
    MEASUREMENT = "measurement"


class DataMode(str, Enum):
    """Profile-level data mode. Never applied to individual parameters."""

    REAL_TIME = "R"
    DELAYED = "D"


class ProfileDirection(str, Enum):
    """Profile vertical trajectory direction."""

    ASCENDING = "A"
    DESCENDING = "D"


class MeasurementValues(str, Enum):
    RAW = "raw"
    ADJUSTED = "adjusted"


class ValidationStatus(str, Enum):
    VALID = "valid"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class IssueCode(str, Enum):
    """Structured warning codes produced by a VALID result."""

    LIMIT_CLAMPED = "limit_clamped"
    DUPLICATE_WMO_NUMBER = "duplicate_wmo_number"
    DUPLICATE_CYCLE_NUMBER = "duplicate_cycle_number"


# ---------------------------------------------------------------------------
# Documented limits and policies
# ---------------------------------------------------------------------------

#: Default number of records returned for list-style chat answers.
DEFAULT_LIST_LIMIT = 5

#: Hard ceiling on records returned to the chat.
MAX_RESULT_LIMIT = 100

#: Ceiling on profiles inspected by one aggregate request. This is a scan
#: budget, not a result count, and must not be confused with MAX_RESULT_LIMIT.
MAX_AGGREGATE_PROFILE_SCAN = 500

#: Maximum number of float identifiers accepted in one specification.
MAX_WMO_NUMBERS = 5

MIN_WMO_NUMBER = 1_000_000
MAX_WMO_NUMBER = 9_999_999
MAX_CYCLE_NUMBER = 10_000

MIN_PRESSURE_DBAR = 0.0
MAX_PRESSURE_DBAR = 11_000.0

#: ARGO reference table 2: measurement-level QC flags (`*_QC` arrays).
#:
#: Flags are numeric 0-9.  Within this project's policy, **flags 6 and 7 are
#: reserved / not currently used** and are never treated as good quality.
#: The good measurement policy is exactly {1, 2} (see
#: ``GOOD_MEASUREMENT_QC_FLAGS``), and the good profile policy is exactly
#: {A, B} (see ``GOOD_PROFILE_QC_FLAGS``); the two scopes are never mixed.
MEASUREMENT_QC_FLAGS = frozenset("0123456789")

#: ARGO reference table 2a: overall profile QC codes (`PROFILE_*_QC`).
PROFILE_QC_FLAGS = frozenset({"A", "B", "C", "D", "E", "F"})

#: Documented "no QC performed" profile code: a single space in table 2a.
#: Kept as an explicit value so it can never be confused with an empty filter.
PROFILE_QC_NO_QC = " "

#: Every profile-level value a QC filter may request.
PROFILE_QC_VALUES = frozenset(PROFILE_QC_FLAGS | {PROFILE_QC_NO_QC})

#: Measurement-level good-quality policy: flags 1 (good) and 2 (probably good).
GOOD_MEASUREMENT_QC_FLAGS = frozenset({"1", "2"})

#: Profile-level good-quality policy: A (100% good levels) and B (75-100%).
#: This selects whole profiles by their percentage of good levels; it is NOT
#: equivalent to filtering every measurement to flags 1 and 2.
GOOD_PROFILE_QC_FLAGS = frozenset({"A", "B"})

#: Backwards-compatible name for the measurement-level policy.
GOOD_QC_FLAGS = GOOD_MEASUREMENT_QC_FLAGS


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeRange:
    """Inclusive profile-time bounds. Either side may be omitted."""

    start: datetime | None = None
    end: datetime | None = None


@dataclass(frozen=True)
class BoundingBox:
    """Explicit latitude/longitude box; named regions are not modelled here."""

    min_latitude: float | None = None
    max_latitude: float | None = None
    min_longitude: float | None = None
    max_longitude: float | None = None


@dataclass(frozen=True)
class RadiusSearch:
    """An explicit "near (lat, lon) within radius" request.

    Radius querying is not implemented in Phase 3C, so a valid instance of this
    value object validates as UNSUPPORTED. It exists so the parser can record
    the user's intent instead of silently turning it into a bounding box.
    """

    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None


@dataclass(frozen=True)
class QcFilter:
    """A QC filter bound to exactly one scope.

    ``scope=PROFILE`` applies to the profile-level QC codes
    (``profile_pres_qc``/``profile_temp_qc``/``profile_psal_qc``).
    ``scope=MEASUREMENT`` applies to the per-level QC arrays and therefore
    requires at least one requested parameter.
    """

    scope: QcScope = QcScope.MEASUREMENT
    flags: frozenset[str] = field(default_factory=lambda: GOOD_QC_FLAGS)


def good_quality_filter(
    scope: QcScope = QcScope.MEASUREMENT,
    flags: frozenset[str] | None = None,
) -> QcFilter:
    """Return the scope-appropriate default good-quality filter.

    Measurement scope uses ARGO flags 1 and 2. Profile scope uses codes A and
    B, meaning at least 75% of levels are good; that is a whole-profile
    selection, not a per-measurement 1/2 filter.
    """

    resolved_scope = (
        scope.value if isinstance(scope, QcScope) else str(scope).strip().lower()
    )

    if flags is None:
        flags = (
            GOOD_PROFILE_QC_FLAGS
            if resolved_scope == QcScope.PROFILE.value
            else GOOD_MEASUREMENT_QC_FLAGS
        )

    return QcFilter(scope=scope, flags=frozenset(flags))


@dataclass(frozen=True)
class SortSpec:
    field: SortField = SortField.PROFILE_TIME
    direction: SortDirection = SortDirection.DESC


@dataclass(frozen=True)
class CapabilityProfile:
    """Optional, caller-supplied description of a dataset's contents.

    ``available_parameters=None`` means "unknown": the validator then makes no
    claim about availability. Supplying a frozenset states *dataset-level*
    presence only; whether a particular profile holds usable measurements is
    resolved later, during execution.
    """

    available_parameters: frozenset[Parameter] | None = None

    def __post_init__(self) -> None:
        if self.available_parameters is None:
            return

        values = self.available_parameters
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(
            values, (frozenset, set, tuple, list)
        ):
            raise ValueError(
                "CapabilityProfile.available_parameters must be a collection "
                "of Parameter members or identifiers, or None."
            )

        normalised = set()
        for value in values:
            parameter = _coerce_enum(value, Parameter)
            if parameter is None:
                raise ValueError(
                    "Unsupported parameter identifier in CapabilityProfile: "
                    f"{value!r}."
                )
            normalised.add(parameter)

        object.__setattr__(self, "available_parameters", frozenset(normalised))

    def supports(self, parameter: Parameter) -> bool | None:
        """True/False when known, None when availability is unspecified."""

        if self.available_parameters is None:
            return None
        return parameter in self.available_parameters


# ---------------------------------------------------------------------------
# The specification itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuerySpec:
    """A structured, validated description of one ARGO query.

    The specification contains *no* SQL and *no* result data. It is the only
    object the execution layer (Phase 3C-D) will be allowed to consume.
    """

    intent: Intent

    parameters: tuple[Parameter, ...] = ()
    wmo_numbers: tuple[int, ...] = ()
    cycle_numbers: tuple[int, ...] = ()

    time_range: TimeRange | None = None
    bounding_box: BoundingBox | None = None

    #: Measurement pressure bounds in dbar, inclusive.
    pressure_range: tuple[float, float] | None = None

    #: Set only when the user explicitly asked in metres/depth. Kept separate
    #: from ``pressure_range`` so no silent conversion can occur.
    depth_range_m: tuple[float, float] | None = None
    depth_requested: bool = False

    #: Explicit radius request; unsupported in this phase.
    radius_search: RadiusSearch | None = None

    qc_filter: QcFilter | None = None
    data_mode: DataMode | None = None
    values: MeasurementValues = MeasurementValues.RAW

    #: Optional BGC product filter: True = BGC profiles, False = core-only.
    has_bgc: bool | None = None

    #: Optional trajectory direction filter ('A' ascending, 'D' descending).
    direction: ProfileDirection | None = None

    #: True when parameter availability targets distinct floats rather than profiles.
    float_level: bool = False

    #: Optional numeric threshold / range filter (Phase 3B).
    numeric_filter: NumericFilter | None = None

    aggregations: tuple[Aggregation, ...] = ()
    group_by: GroupBy | None = None

    sort: SortSpec = field(default_factory=SortSpec)
    limit: int | None = None

    #: Free text for the semantic-discovery fallback path.
    semantic_query: str | None = None

    #: Human-readable interpretation notes (never used for validation).
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalise valid sequences so a constructed spec cannot hold lists.

        Invalid types (scalars, strings, ``None``) are deliberately left for
        ``validate_spec`` to report as INVALID rather than raising here.
        """

        for name in (
            "parameters",
            "wmo_numbers",
            "cycle_numbers",
            "aggregations",
            "pressure_range",
            "depth_range_m",
            "notes",
        ):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True)
class ValidationWarning:
    """A structured, non-fatal observation about a VALID specification."""

    code: IssueCode
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a QuerySpec.

    ``spec`` is populated only for VALID results. Invalid, unsupported and
    clarification outcomes carry a human-readable ``reason`` instead.
    """

    status: ValidationStatus
    spec: QuerySpec | None = None
    reason: str | None = None
    warnings: tuple[ValidationWarning, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.VALID


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _require_sequence(value, field: str) -> tuple[object, str | None]:
    """Return ``(sequence, error)`` for a field that must be a collection.

    Strings are rejected explicitly instead of being iterated character by
    character, and scalars/``None`` produce a field-specific message.
    """

    if isinstance(value, (str, bytes, bytearray)):
        return (), f"{field} must be a tuple or list, not a string."
    if value is None:
        return (), f"{field} must be a tuple or list, not None."
    if not isinstance(value, (tuple, list)):
        return (), (
            f"{field} must be a tuple or list, got {type(value).__name__}."
        )
    return value, None


def _to_tuple(value):
    """Convert a sequence to a tuple; leave non-sequences untouched."""

    if isinstance(value, (tuple, list)):
        return tuple(value)
    return value


def _coerce_enum(value: object, enum_type: type[Enum]) -> Enum | None:
    """Accept an enum member, its value, or a case-insensitive name."""

    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        for member in enum_type:
            if candidate == member.value or candidate.upper() == member.name:
                return member
    return None


def _normalise_parameters(
    values,
) -> tuple[tuple[Parameter, ...], list[str], list[str]]:
    """Split parameter input into accepted, malformed and unsupported parts.

    A textual identifier the application does not recognise is well-formed but
    unsupported (UNSUPPORTED); a non-textual value is malformed (INVALID).
    """

    parameters: list[Parameter] = []
    errors: list[str] = []
    unsupported: list[str] = []

    for value in values:
        parameter = _coerce_enum(value, Parameter)
        if parameter is not None:
            if parameter not in parameters:
                parameters.append(parameter)
            continue
        if isinstance(value, str):
            unsupported.append(f"Unsupported parameter identifier: {value!r}.")
        else:
            errors.append(f"Parameter must be a Parameter or string, got {value!r}.")

    return tuple(parameters), errors, unsupported


def _normalise_wmos(values) -> tuple[tuple[int, ...], list[str], bool]:
    numbers: list[int] = []
    errors: list[str] = []
    duplicated = False

    for value in values:
        if not _is_int(value):
            errors.append(f"WMO number must be an integer, got {value!r}.")
            continue
        if not MIN_WMO_NUMBER <= value <= MAX_WMO_NUMBER:
            errors.append(
                f"WMO number {value} is not a valid seven-digit identifier."
            )
            continue
        if value in numbers:
            duplicated = True
            continue
        numbers.append(value)

    if len(numbers) > MAX_WMO_NUMBERS:
        errors.append(
            f"At most {MAX_WMO_NUMBERS} WMO numbers may be compared at once."
        )

    return tuple(numbers), errors, duplicated


def _normalise_cycles(values) -> tuple[tuple[int, ...], list[str], bool]:
    cycles: list[int] = []
    errors: list[str] = []
    duplicated = False

    for value in values:
        if not _is_int(value):
            errors.append(f"Cycle number must be an integer, got {value!r}.")
            continue
        if value < 0:
            errors.append(f"Cycle number cannot be negative: {value}.")
            continue
        if value > MAX_CYCLE_NUMBER:
            errors.append(
                f"Cycle number {value} exceeds the supported maximum "
                f"{MAX_CYCLE_NUMBER}."
            )
            continue
        if value in cycles:
            duplicated = True
            continue
        cycles.append(value)

    return tuple(cycles), errors, duplicated


def _normalise_aggregations(values) -> tuple[tuple[Aggregation, ...], list[str]]:
    operations: list[Aggregation] = []
    errors: list[str] = []

    for value in values:
        operation = _coerce_enum(value, Aggregation)
        if operation is None:
            errors.append(f"Unsupported aggregation operation: {value!r}.")
            continue
        if operation in operations:
            errors.append(
                f"Duplicate aggregation operation: {operation.value}."
            )
            continue
        operations.append(operation)

    return tuple(operations), errors


def _normalise_limit(value) -> tuple[int | None, list[str], list[ValidationWarning]]:
    if value is None:
        return None, [], []

    if not _is_int(value):
        return None, [f"Result limit must be an integer, got {value!r}."], []

    if value < 1:
        return None, ["Result limit must be at least 1."], []

    if value > MAX_RESULT_LIMIT:
        warning = ValidationWarning(
            code=IssueCode.LIMIT_CLAMPED,
            message=(
                f"Requested limit {value} exceeds the maximum of "
                f"{MAX_RESULT_LIMIT}; it has been clamped."
            ),
        )
        return MAX_RESULT_LIMIT, [], [warning]

    return value, [], []


def _validate_time_range(time_range) -> list[str]:
    if time_range is None:
        return []

    if not isinstance(time_range, TimeRange):
        return ["Time range must be a TimeRange instance."]

    errors: list[str] = []

    for label, value in (("start", time_range.start), ("end", time_range.end)):
        if value is None:
            continue
        if not isinstance(value, datetime):
            errors.append(f"Time range {label} must be a datetime.")

    if errors:
        return errors

    if time_range.start and time_range.end:
        try:
            if time_range.end < time_range.start:
                errors.append("Time range end cannot be earlier than its start.")
        except TypeError:
            errors.append(
                "Time range bounds must both be timezone-aware or both be "
                "timezone-naive."
            )

    return errors


def _validate_bounding_box(box) -> list[str]:
    if box is None:
        return []

    if not isinstance(box, BoundingBox):
        return ["Bounding box must be a BoundingBox instance."]

    errors: list[str] = []
    values = {
        "min_latitude": box.min_latitude,
        "max_latitude": box.max_latitude,
        "min_longitude": box.min_longitude,
        "max_longitude": box.max_longitude,
    }

    missing = [name for name, value in values.items() if value is None]
    if missing:
        return [
            "Bounding box is incomplete; missing " + ", ".join(missing) + "."
        ]

    for name, value in values.items():
        if not _is_number(value):
            errors.append(f"Bounding box {name} must be a finite number.")

    if errors:
        return errors

    if not -90.0 <= box.min_latitude <= 90.0 or not -90.0 <= box.max_latitude <= 90.0:
        errors.append("Latitude must lie between -90 and 90 degrees.")
    if not -180.0 <= box.min_longitude <= 180.0 or not -180.0 <= box.max_longitude <= 180.0:
        errors.append("Longitude must lie between -180 and 180 degrees.")

    if box.min_latitude > box.max_latitude:
        errors.append("min_latitude cannot be greater than max_latitude.")
    if box.min_longitude > box.max_longitude:
        errors.append("min_longitude cannot be greater than max_longitude.")

    return errors


def _validate_radius(radius) -> list[str]:
    if radius is None:
        return []

    if not isinstance(radius, RadiusSearch):
        return ["Radius search must be a RadiusSearch instance."]

    errors: list[str] = []

    if radius.latitude is None or radius.longitude is None:
        errors.append("A radius search requires an explicit centre coordinate.")
    else:
        if not _is_number(radius.latitude) or not -90.0 <= radius.latitude <= 90.0:
            errors.append("Radius search latitude must lie between -90 and 90.")
        if not _is_number(radius.longitude) or not -180.0 <= radius.longitude <= 180.0:
            errors.append(
                "Radius search longitude must lie between -180 and 180."
            )

    if radius.radius_km is None or not _is_number(radius.radius_km) or radius.radius_km <= 0:
        errors.append("A radius search requires an explicit positive radius in km.")

    return errors


def _validate_pressure_range(pressure_range) -> list[str]:
    if pressure_range is None:
        return []

    if not isinstance(pressure_range, (tuple, list)) or len(pressure_range) != 2:
        return ["Pressure range must be a (minimum, maximum) pair."]

    minimum, maximum = pressure_range
    errors: list[str] = []

    if not _is_number(minimum) or not _is_number(maximum):
        return ["Pressure range bounds must be finite numbers."]

    if minimum < MIN_PRESSURE_DBAR:
        errors.append("Pressure cannot be negative.")
    if maximum > MAX_PRESSURE_DBAR:
        errors.append(
            f"Pressure above {MAX_PRESSURE_DBAR:g} dbar is not plausible."
        )
    if minimum > maximum:
        errors.append(
            "Pressure range minimum cannot be greater than its maximum."
        )

    return errors


def _validate_depth(depth_range_m) -> list[str]:
    if depth_range_m is None:
        return []

    if not isinstance(depth_range_m, (tuple, list)) or len(depth_range_m) != 2:
        return ["Depth range must be a (minimum, maximum) pair in metres."]

    minimum, maximum = depth_range_m

    if not _is_number(minimum) or not _is_number(maximum):
        return ["Depth range bounds must be finite numbers."]

    errors: list[str] = []
    if minimum < 0:
        errors.append("Depth cannot be negative.")
    if minimum > maximum:
        errors.append("Depth range minimum cannot be greater than its maximum.")

    return errors


def _validate_qc_filter(
    qc_filter, parameters: tuple[Parameter, ...]
) -> tuple[list[str], QcFilter | None]:
    if qc_filter is None:
        return [], None

    if not isinstance(qc_filter, QcFilter):
        return ["QC filter must be a QcFilter instance."], None

    scope = _coerce_enum(qc_filter.scope, QcScope)
    if scope is None:
        return ["QC scope must be either 'profile' or 'measurement'."], None

    flags = qc_filter.flags
    if isinstance(flags, str) or not isinstance(
        flags, (frozenset, set, tuple, list)
    ):
        return [
            "QC filter flags must be a collection of flag characters."
        ], None

    if not flags:
        # An empty collection is not a "no QC" request.
        return ["QC filter must contain at least one flag."], None

    allowed = (
        PROFILE_QC_VALUES
        if scope is QcScope.PROFILE
        else MEASUREMENT_QC_FLAGS
    )

    normalised: set[str] = set()
    invalid_flags: list[str] = []

    for flag in flags:
        if not isinstance(flag, str) or len(flag) != 1:
            invalid_flags.append(repr(flag))
            continue
        candidate = flag.upper()
        if candidate not in allowed:
            invalid_flags.append(repr(flag))
            continue
        normalised.add(candidate)

    if invalid_flags:
        scale = (
            "A-F, or the single-space no-QC marker"
            if scope is QcScope.PROFILE
            else "0-9"
        )
        return [
            f"Invalid {scope.value}-level QC flag(s): "
            + ", ".join(sorted(invalid_flags))
            + f". {scope.value.capitalize()}-level QC accepts {scale}."
        ], None

    if scope is QcScope.MEASUREMENT and not parameters:
        return [
            "Measurement-level QC requires at least one requested parameter."
        ], None

    return [], QcFilter(scope=scope, flags=frozenset(normalised))


def _validate_values(values) -> list[str]:
    if _coerce_enum(values, MeasurementValues) is None:
        return ["Measurement selection must be 'raw' or 'adjusted'."]
    return []


# ---------------------------------------------------------------------------
# Public validation entry point
# ---------------------------------------------------------------------------


def validate_spec(
    spec: QuerySpec,
    capabilities: CapabilityProfile | None = None,
) -> ValidationResult:
    """Validate one specification without touching any external service.

    Status precedence, documented for callers:

    1. ``INVALID`` - malformed input (bad types, reversed ranges, bad flags).
    2. ``UNSUPPORTED`` - well-formed request the current phase cannot execute
       (radius searches, dataset lacking a requested parameter).
    3. ``NEEDS_CLARIFICATION`` - well-formed but scientifically ambiguous
       (depth in metres).
    4. ``VALID`` - executable; ``spec`` is populated and normalised.

    Warnings (clamped limit, de-duplicated identifiers) are only produced for
    VALID results.
    """

    if not isinstance(spec, QuerySpec):
        return ValidationResult(
            status=ValidationStatus.INVALID,
            reason="The query specification must be a QuerySpec instance.",
        )

    errors: list[str] = []
    unsupported: list[str] = []
    clarifications: list[str] = []
    warnings: list[ValidationWarning] = []

    # --- intent -----------------------------------------------------------
    intent = _coerce_enum(spec.intent, Intent)
    if intent is None:
        errors.append("Unknown intent; expected a supported Intent value.")

    # --- parameters -------------------------------------------------------
    parameter_values, sequence_error = _require_sequence(
        spec.parameters, "parameters"
    )
    if sequence_error:
        errors.append(sequence_error)
        parameters = ()
    else:
        parameters, parameter_errors, parameter_unsupported = (
            _normalise_parameters(parameter_values)
        )
        errors.extend(parameter_errors)
        unsupported.extend(parameter_unsupported)

    if capabilities is not None and not isinstance(
        capabilities, CapabilityProfile
    ):
        errors.append(
            "capabilities must be a CapabilityProfile instance or None."
        )
        capabilities = None

    if capabilities is not None and capabilities.available_parameters is not None:
        missing = [
            parameter
            for parameter in parameters
            if not capabilities.supports(parameter)
        ]
        if missing:
            unsupported.append(
                "The supplied dataset does not contain: "
                + ", ".join(parameter.value for parameter in missing)
                + ". Recognising a parameter does not mean measurements exist."
            )

    # --- identifiers ------------------------------------------------------
    wmo_values, sequence_error = _require_sequence(
        spec.wmo_numbers, "wmo_numbers"
    )
    if sequence_error:
        errors.append(sequence_error)
        wmo_numbers, duplicate_wmo = (), False
    else:
        wmo_numbers, wmo_errors, duplicate_wmo = _normalise_wmos(wmo_values)
        errors.extend(wmo_errors)
    if duplicate_wmo:
        warnings.append(
            ValidationWarning(
                code=IssueCode.DUPLICATE_WMO_NUMBER,
                message="Duplicate WMO numbers were collapsed to one entry.",
            )
        )

    cycle_values, sequence_error = _require_sequence(
        spec.cycle_numbers, "cycle_numbers"
    )
    if sequence_error:
        errors.append(sequence_error)
        cycle_numbers, duplicate_cycle = (), False
    else:
        cycle_numbers, cycle_errors, duplicate_cycle = _normalise_cycles(
            cycle_values
        )
        errors.extend(cycle_errors)
    if duplicate_cycle:
        warnings.append(
            ValidationWarning(
                code=IssueCode.DUPLICATE_CYCLE_NUMBER,
                message="Duplicate cycle numbers were collapsed to one entry.",
            )
        )

    # --- filters ----------------------------------------------------------
    errors.extend(_validate_time_range(spec.time_range))
    errors.extend(_validate_bounding_box(spec.bounding_box))

    pressure_errors = _validate_pressure_range(spec.pressure_range)
    errors.extend(pressure_errors)

    qc_errors, normalised_qc = _validate_qc_filter(spec.qc_filter, parameters)
    errors.extend(qc_errors)

    radius_errors = _validate_radius(spec.radius_search)
    errors.extend(radius_errors)
    if spec.radius_search is not None and not radius_errors:
        unsupported.append(
            "Radius-based searching is not supported yet; provide an explicit "
            "latitude/longitude bounding box instead."
        )

    # Depth is a scientific clarification, not a silent conversion.
    depth_errors = _validate_depth(spec.depth_range_m)
    errors.extend(depth_errors)
    if not depth_errors and (
        spec.depth_requested or spec.depth_range_m is not None
    ):
        if spec.pressure_range is not None and not pressure_errors:
            clarifications.append(
                "You supplied both a depth range in metres and a pressure "
                "range in dbar. ARGO stores pressure (dbar), not depth, and "
                "the two are never converted automatically. Which range "
                "should be applied?"
            )
        else:
            clarifications.append(
                "Depth is not stored: ARGO measures pressure in dbar. Please "
                "give a pressure range (for example 0-500 dbar); depth in "
                "metres is not converted automatically."
            )

    # --- measurement selection, mode, BGC filter & direction --------------
    if spec.data_mode is not None and _coerce_enum(spec.data_mode, DataMode) is None:
        errors.append("Data mode must be 'R' (real-time) or 'D' (delayed).")

    if spec.has_bgc is not None and not isinstance(spec.has_bgc, bool):
        errors.append("BGC filter (has_bgc) must be True, False, or None.")

    if spec.has_bgc is False and any(p in BGC_PARAMETERS for p in parameters):
        errors.append(
            "Core-only filter (has_bgc=False) cannot be combined with BGC parameters."
        )

    normalised_direction = None
    if spec.direction is not None:
        normalised_direction = _coerce_enum(spec.direction, ProfileDirection)
        if normalised_direction is None:
            errors.append(
                "Profile direction must be 'A' (ascending) or 'D' (descending)."
            )

    if not isinstance(spec.float_level, bool):
        errors.append("float_level must be a boolean.")

    normalised_numeric_filter = None
    if spec.numeric_filter is not None:
        nf = spec.numeric_filter
        if not isinstance(nf, NumericFilter):
            errors.append("numeric_filter must be a NumericFilter instance.")
        else:
            nf_param = _coerce_enum(nf.parameter, Parameter)
            nf_op = _coerce_enum(nf.operator, ComparisonOperator)
            nf_agg = (
                _coerce_enum(nf.aggregation, Aggregation)
                if nf.aggregation is not None
                else None
            )
            if nf_param is None:
                errors.append("numeric_filter parameter is not supported.")
            if nf_op is None:
                errors.append("numeric_filter operator is not supported.")
            if not _is_number(nf.value):
                errors.append("numeric_filter value must be a finite number.")
            if nf_op is ComparisonOperator.BETWEEN:
                if not _is_number(nf.upper_value):
                    errors.append(
                        "BETWEEN numeric_filter requires a finite upper_value."
                    )
                elif _is_number(nf.value) and float(nf.value) > float(nf.upper_value):
                    errors.append(
                        "BETWEEN lower bound cannot exceed upper bound."
                    )
            if nf.scope not in ("measurement", "profile", "float"):
                errors.append(
                    "numeric_filter scope must be 'measurement', 'profile', or 'float'."
                )
            if nf.aggregation is not None and nf_agg is None:
                errors.append("numeric_filter aggregation is not supported.")
            if not errors:
                normalised_numeric_filter = NumericFilter(
                    parameter=nf_param,
                    operator=nf_op,
                    value=float(nf.value),
                    upper_value=(
                        float(nf.upper_value)
                        if nf.upper_value is not None
                        else None
                    ),
                    scope=nf.scope,
                    aggregation=nf_agg,
                    unit=nf.unit,
                )

    errors.extend(_validate_values(spec.values))
    if _coerce_enum(spec.values, MeasurementValues) is MeasurementValues.ADJUSTED and not parameters:
        errors.append(
            "Adjusted measurements require at least one requested parameter."
        )

    # --- aggregation and grouping ----------------------------------------
    aggregation_values, sequence_error = _require_sequence(
        spec.aggregations, "aggregations"
    )
    if sequence_error:
        errors.append(sequence_error)
        aggregations = ()
    else:
        aggregations, aggregation_errors = _normalise_aggregations(
            aggregation_values
        )
        errors.extend(aggregation_errors)

    value_operations = [
        operation
        for operation in aggregations
        if operation is not Aggregation.COUNT
    ]
    if value_operations and not parameters:
        errors.append(
            "MEAN, MIN and MAX require at least one requested parameter; "
            "COUNT does not."
        )

    group_by = None
    if spec.group_by is not None:
        group_by = _coerce_enum(spec.group_by, GroupBy)
        if group_by is None:
            errors.append("Unsupported grouping dimension.")
        elif not aggregations:
            errors.append("A grouping dimension requires an aggregation.")

    # --- sorting and limits ----------------------------------------------
    sort = spec.sort
    if not isinstance(sort, SortSpec):
        errors.append("Sort must be a SortSpec instance.")
    else:
        if _coerce_enum(sort.field, SortField) is None:
            errors.append("Unsupported sort field.")
        if _coerce_enum(sort.direction, SortDirection) is None:
            errors.append("Sort direction must be 'asc' or 'desc'.")

    limit, limit_errors, limit_warnings = _normalise_limit(spec.limit)
    errors.extend(limit_errors)
    warnings.extend(limit_warnings)

    # --- intent-specific rules -------------------------------------------
    if intent is Intent.SEMANTIC_DISCOVERY:
        if not isinstance(spec.semantic_query, str) or not spec.semantic_query.strip():
            errors.append(
                "Semantic discovery requires non-empty search text."
            )

    if intent is Intent.CONVERSATIONAL:
        has_filters = any(
            [
                parameters,
                wmo_numbers,
                cycle_numbers,
                spec.time_range,
                spec.bounding_box,
                spec.pressure_range,
                spec.depth_range_m,
                spec.radius_search,
                spec.qc_filter,
                spec.data_mode,
                spec.has_bgc is not None,
                spec.direction is not None,
                spec.float_level,
                spec.numeric_filter is not None,
                aggregations,
                spec.group_by,
            ]
        )
        if has_filters:
            errors.append(
                "A conversational message cannot carry data filters."
            )

    if intent is Intent.AGGREGATE and not aggregations:
        errors.append("An aggregate query requires at least one operation.")

    if intent is Intent.COMPARE:
        if (
            len(wmo_numbers) < 2
            and len(cycle_numbers) < 2
            and len(parameters) < 2
            and not (len(parameters) == 1 and parameters[0] in BGC_PARAMETERS)
        ):
            errors.append(
                "A comparison requires at least two distinct WMO numbers, "
                "cycle numbers, or parameters."
            )

    if intent is Intent.PARAMETER_AVAILABILITY and not parameters:
        errors.append(
            "Parameter availability requires at least one requested parameter."
        )

    # --- outcome ----------------------------------------------------------
    if errors:
        return ValidationResult(
            status=ValidationStatus.INVALID,
            reason=" ".join(errors),
        )

    if unsupported:
        return ValidationResult(
            status=ValidationStatus.UNSUPPORTED,
            reason=" ".join(unsupported),
        )

    if clarifications:
        return ValidationResult(
            status=ValidationStatus.NEEDS_CLARIFICATION,
            reason=" ".join(clarifications),
        )

    normalised = replace(
        spec,
        intent=intent,
        parameters=parameters,
        wmo_numbers=wmo_numbers,
        cycle_numbers=cycle_numbers,
        aggregations=aggregations,
        group_by=group_by,
        qc_filter=normalised_qc,
        limit=limit,
        data_mode=(
            None
            if spec.data_mode is None
            else _coerce_enum(spec.data_mode, DataMode)
        ),
        has_bgc=spec.has_bgc,
        direction=normalised_direction,
        float_level=spec.float_level,
        numeric_filter=normalised_numeric_filter,
        pressure_range=_to_tuple(spec.pressure_range),
        depth_range_m=_to_tuple(spec.depth_range_m),
        notes=_to_tuple(spec.notes),
        values=_coerce_enum(spec.values, MeasurementValues),
        sort=SortSpec(
            field=_coerce_enum(sort.field, SortField),
            direction=_coerce_enum(sort.direction, SortDirection),
        ),
    )

    return ValidationResult(
        status=ValidationStatus.VALID,
        spec=normalised,
        warnings=tuple(warnings),
    )
