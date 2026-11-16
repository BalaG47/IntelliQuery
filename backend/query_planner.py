"""Deterministic QuerySpec -> QueryPlan conversion for IntelliQuery.

Phase 3C-D scope: planning only, and an optional controlled executor.  This
module:

* accepts a ``QuerySpec`` (or an existing ``ValidationResult``),
* validates it with ``query_spec.validate_spec`` (no duplicated rules),
* produces an inspectable ``QueryPlan`` with fixed SQL templates and bound
  parameters,
* refuses every filter it cannot represent against the verified schema.

It contains no natural-language parsing, no LLM calls, no semantic retrieval
and no production chat integration.  Table and column names come only from the
fixed maps below; user values only ever appear as bound parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from query_spec import (
    DEFAULT_LIST_LIMIT,
    MAX_AGGREGATE_PROFILE_SCAN,
    MAX_RESULT_LIMIT,
    Aggregation,
    DataMode,
    GroupBy,
    Intent,
    MeasurementValues,
    Parameter,
    QcScope,
    QuerySpec,
    SortDirection,
    SortField,
    ValidationResult,
    ValidationStatus,
    validate_spec,
)


# ---------------------------------------------------------------------------
# Verified schema maps (see PHASE_3C_D report, section 3)
# ---------------------------------------------------------------------------

_SORT_COLUMNS = {
    SortField.PROFILE_TIME: "p.profile_time",
    SortField.CYCLE_NUMBER: "p.cycle_number",
    SortField.WMO_NUMBER: "f.wmo_number",
    SortField.LATITUDE: "p.latitude",
    SortField.LONGITUDE: "p.longitude",
    SortField.PROFILE_ID: "p.profile_id",
}

_CORE_PARAMETER_COLUMNS = {
    MeasurementValues.RAW: {
        Parameter.TEMP: "temperature",
        Parameter.PSAL: "salinity",
        Parameter.PRES: "pressure",
    },
    MeasurementValues.ADJUSTED: {
        Parameter.TEMP: "temperature_adjusted",
        Parameter.PSAL: "salinity_adjusted",
        Parameter.PRES: "pressure_adjusted",
    },
}

_BGC_PARAMETER_KEYS = {
    Parameter.DOXY: "DOXY",
    Parameter.CHLA: "CHLA",
    Parameter.BBP700: "BBP700",
    Parameter.NITRATE: "NITRATE",
    Parameter.PH_IN_SITU_TOTAL: "PH_IN_SITU_TOTAL",
}

_PROFILE_QC_COLUMNS = {
    Parameter.PRES: "profile_pres_qc",
    Parameter.TEMP: "profile_temp_qc",
    Parameter.PSAL: "profile_psal_qc",
}

#: Fixed template identifiers; execution only ever sees SQL built here.
TEMPLATE_IDS = (
    "enumerate_floats",
    "count_profiles",
    "list_profiles",
    "profile_detail",
    "parameter_availability",
    "metadata_info",
    "no_database_query",
)


class PlanOperation(str, Enum):
    ENUMERATE_FLOATS = "enumerate_floats"
    COUNT_PROFILES = "count_profiles"
    LIST_PROFILES = "list_profiles"
    PROFILE_DETAIL = "profile_detail"
    PARAMETER_AVAILABILITY = "parameter_availability"
    METADATA_INFO = "metadata_info"
    NO_DATABASE_QUERY = "no_database_query"
    UNSUPPORTED = "unsupported"


class ResultShape(str, Enum):
    FLOAT_ROWS = "float_rows"
    COUNT = "count"
    COUNT_BY_GROUP = "count_by_group"
    PROFILE_ROWS = "profile_rows"
    PROFILE_DETAIL = "profile_detail"
    AVAILABILITY_ROWS = "availability_rows"
    FLOAT_METADATA = "float_metadata"
    NONE = "none"


@dataclass(frozen=True)
class QueryPlan:
    """One inspectable execution plan.

    ``supported`` plans carry a fixed template id, SQL and bound parameters.
    An unsupported plan carries only a reason; it is never executable.
    """

    operation: PlanOperation
    template_id: str
    supported: bool
    sql: str | None = None
    params: dict = field(default_factory=dict)
    limit: int | None = None
    scan_limit: int | None = None
    result_shape: ResultShape = ResultShape.NONE
    post_processing: tuple[str, ...] = ()
    reason: str | None = None
    uses_semantic_retrieval: bool = False


def _unplan(reason: str, *, operation: PlanOperation = PlanOperation.UNSUPPORTED,
            semantic: bool = False) -> QueryPlan:
    return QueryPlan(
        operation=operation,
        template_id="unsupported",
        supported=False,
        reason=reason,
        uses_semantic_retrieval=semantic,
    )


def plan_query(spec: QuerySpec, capabilities=None) -> QueryPlan:
    """Validate a QuerySpec and return its plan.

    Validation is delegated to ``query_spec.validate_spec`` so the planner never
    duplicates or weakens QuerySpec rules.
    """

    return plan_validation(validate_spec(spec, capabilities))


def plan_validation(validation: ValidationResult, capabilities=None) -> QueryPlan:
    """Convert an existing ValidationResult into a QueryPlan.

    A constructed ``ValidationResult`` is only a claim; it is not a trusted
    token.  The embedded spec is therefore validated again here so a forged or
    stale VALID result cannot bypass the validation boundary.  Callers that
    validated against a ``CapabilityProfile`` must pass the same capabilities
    again.
    """

    if not isinstance(validation, ValidationResult):
        raise TypeError("plan_validation expects a ValidationResult.")

    if validation.status is not ValidationStatus.VALID or validation.spec is None:
        return _unplan(
            "The query specification is not valid and cannot be planned: "
            + (validation.reason or validation.status.value)
        )

    if not isinstance(validation.spec, QuerySpec):
        return _unplan(
            "The validation result does not contain a QuerySpec and cannot be "
            "planned."
        )

    revalidated = validate_spec(validation.spec, capabilities)
    if revalidated.status is not ValidationStatus.VALID or revalidated.spec is None:
        return _unplan(
            "The query specification is not valid and cannot be planned: "
            + (revalidated.reason or revalidated.status.value)
        )

    spec = revalidated.spec

    if spec.intent is Intent.ENUMERATE_FLOATS:
        return _plan_enumerate_floats(spec)
    if spec.intent is Intent.COUNT_PROFILES:
        return _plan_count_profiles(spec)
    if spec.intent is Intent.LIST_PROFILES:
        return _plan_list_profiles(spec)
    if spec.intent is Intent.PROFILE_DETAIL:
        return _plan_profile_detail(spec)
    if spec.intent is Intent.PARAMETER_AVAILABILITY:
        return _plan_parameter_availability(spec)
    if spec.intent is Intent.METADATA_INFO:
        return _plan_metadata_info(spec)
    if spec.intent is Intent.AGGREGATE:
        return _plan_aggregate(spec)
    if spec.intent in (Intent.CONVERSATIONAL, Intent.CLARIFY):
        return QueryPlan(
            operation=PlanOperation.NO_DATABASE_QUERY,
            template_id="no_database_query",
            supported=True,
            result_shape=ResultShape.NONE,
        )
    if spec.intent is Intent.COMPARE:
        return _unplan(
            "Comparison execution requires post-processing that is not part of "
            "Phase 3C-D; no SQL was generated."
        )
    if spec.intent is Intent.SEMANTIC_DISCOVERY:
        return _unplan(
            "Semantic discovery requires ChromaDB retrieval, not SQL; the "
            "planner does not execute semantic queries.",
            semantic=True,
        )

    return _unplan("Unsupported query intent.")


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------


def _in_clause(column: str, values, prefix: str, params: dict) -> str:
    names = []
    for index, value in enumerate(values):
        name = f"{prefix}_{index}"
        params[name] = value
        names.append(f":{name}")
    return f"{column} IN ({', '.join(names)})"


def _limit_value(spec: QuerySpec, default: int = DEFAULT_LIST_LIMIT) -> int:
    value = spec.limit if spec.limit is not None else default
    return max(1, min(int(value), MAX_RESULT_LIMIT))


def _has_any_filter(spec: QuerySpec) -> bool:
    return any(
        (
            spec.parameters,
            spec.wmo_numbers,
            spec.cycle_numbers,
            spec.time_range,
            spec.bounding_box,
            spec.pressure_range,
            spec.depth_range_m,
            spec.depth_requested,
            spec.radius_search,
            spec.qc_filter,
            spec.data_mode,
            spec.aggregations,
            spec.group_by,
        )
    )


def _profile_filters(spec: QuerySpec) -> tuple[list[str], dict, list[str]]:
    """Build profile WHERE fragments, or report unsupported filters.

    Returns ``(clauses, params, unsupported_reasons)``.  Callers must not use
    the clauses if any reason is present.
    """

    clauses: list[str] = []
    params: dict = {}
    unsupported: list[str] = []

    if spec.wmo_numbers:
        clauses.append(
            _in_clause("f.wmo_number", spec.wmo_numbers, "wmo", params)
        )
    if spec.cycle_numbers:
        clauses.append(
            _in_clause("p.cycle_number", spec.cycle_numbers, "cycle", params)
        )

    if spec.time_range is not None:
        if spec.time_range.start is not None:
            clauses.append("p.profile_time >= :start_time")
            params["start_time"] = spec.time_range.start
        if spec.time_range.end is not None:
            clauses.append("p.profile_time <= :end_time")
            params["end_time"] = spec.time_range.end

    if spec.bounding_box is not None:
        box = spec.bounding_box
        clauses.extend(
            (
                "p.latitude >= :min_latitude",
                "p.latitude <= :max_latitude",
                "p.longitude >= :min_longitude",
                "p.longitude <= :max_longitude",
            )
        )
        params.update(
            {
                "min_latitude": box.min_latitude,
                "max_latitude": box.max_latitude,
                "min_longitude": box.min_longitude,
                "max_longitude": box.max_longitude,
            }
        )

    if spec.data_mode is not None:
        clauses.append("p.data_mode = :data_mode")
        params["data_mode"] = spec.data_mode.value

    if spec.pressure_range is not None:
        unsupported.append(
            "Pressure filtering applies to individual measurements and "
            "requires per-level post-processing, which Phase 3C-D does not "
            "implement; the filter was not dropped."
        )

    if spec.depth_requested or spec.depth_range_m is not None:
        unsupported.append(
            "Depth is not stored; pressure filtering is required instead."
        )

    if spec.qc_filter is not None:
        qc = spec.qc_filter
        if qc.scope is QcScope.MEASUREMENT:
            unsupported.append(
                "Measurement-level QC requires per-level QC arrays and is not "
                "supported by metadata SQL in Phase 3C-D."
            )
        else:
            if len(spec.parameters) != 1:
                unsupported.append(
                    "Profile-level QC is applied per parameter; exactly one "
                    "core parameter is required."
                )
            else:
                parameter = spec.parameters[0]
                column = _PROFILE_QC_COLUMNS.get(parameter)
                if column is None:
                    unsupported.append(
                        "Profile-level QC columns exist only for PRES, TEMP "
                        "and PSAL."
                    )
                else:
                    flags = sorted(qc.flags)
                    clauses.append(
                        _in_clause(f"p.{column}", flags, "profile_qc", params)
                    )

    if spec.parameters:
        for parameter in spec.parameters:
            core = _CORE_PARAMETER_COLUMNS[spec.values].get(parameter)
            if core is not None:
                clauses.append(
                    f"JSON_LENGTH(COALESCE(p.{core}, '[]')) > 0"
                )
                continue
            key = _BGC_PARAMETER_KEYS.get(parameter)
            if key is None:
                unsupported.append(f"Unsupported parameter: {parameter.value}.")
                continue
            source = "bgc_adjusted" if spec.values is MeasurementValues.ADJUSTED else "bgc_params"
            clauses.append(
                "JSON_CONTAINS_PATH(COALESCE("
                f"p.{source}, '{{}}'), 'one', '$.{key}')"
            )

    return clauses, params, unsupported


def _order_clause(spec: QuerySpec) -> str:
    column = _SORT_COLUMNS.get(spec.sort.field)
    if column is None:
        return "p.profile_time DESC, p.profile_id ASC"
    direction = "DESC" if spec.sort.direction is SortDirection.DESC else "ASC"
    if column == "p.profile_id":
        return f"{column} {direction}"
    return f"{column} {direction}, p.profile_id ASC"


def _where(clauses: list[str]) -> str:
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


# ---------------------------------------------------------------------------
# Operation planners
# ---------------------------------------------------------------------------


def _plan_enumerate_floats(spec: QuerySpec) -> QueryPlan:
    if spec.sort.field not in (SortField.PROFILE_TIME, SortField.WMO_NUMBER):
        return _unplan(
            "Float enumeration supports only WMO ordering in Phase 3C-D."
        )
    if _has_any_filter(spec) and (
        spec.parameters
        or spec.cycle_numbers
        or spec.time_range
        or spec.bounding_box
        or spec.pressure_range
        or spec.depth_requested
        or spec.depth_range_m
        or spec.qc_filter
        or spec.data_mode
        or spec.aggregations
        or spec.group_by
    ):
        return _unplan(
            "Float enumeration supports only the WMO filter in Phase 3C-D."
        )

    clauses: list[str] = []
    params: dict = {}
    if spec.wmo_numbers:
        clauses.append(
            _in_clause("f.wmo_number", spec.wmo_numbers, "wmo", params)
        )

    limit = _limit_value(spec)
    params["limit"] = limit
    sql = (
        "SELECT f.float_id, f.wmo_number, f.project_name, f.platform_type "
        "FROM argo_floats f "
        f"{_where(clauses)} "
        "ORDER BY f.wmo_number ASC "
        "LIMIT :limit"
    )
    return QueryPlan(
        operation=PlanOperation.ENUMERATE_FLOATS,
        template_id="enumerate_floats",
        supported=True,
        sql=sql,
        params=params,
        limit=limit,
        result_shape=ResultShape.FLOAT_ROWS,
    )


def _plan_count_profiles(spec: QuerySpec) -> QueryPlan:
    if spec.aggregations or spec.group_by is not None:
        return _unplan(
            "COUNT_PROFILES is a profile count; supply aggregation through the "
            "AGGREGATE intent instead."
        )

    clauses, params, unsupported = _profile_filters(spec)
    if unsupported:
        return _unplan(" ".join(unsupported))

    sql = (
        "SELECT COUNT(*) AS profile_count "
        "FROM argo_profiles p "
        "JOIN argo_floats f ON p.float_id = f.float_id "
        f"{_where(clauses)}"
    )
    return QueryPlan(
        operation=PlanOperation.COUNT_PROFILES,
        template_id="count_profiles",
        supported=True,
        sql=sql,
        params=params,
        scan_limit=MAX_AGGREGATE_PROFILE_SCAN,
        result_shape=ResultShape.COUNT,
    )


def _plan_list_profiles(spec: QuerySpec) -> QueryPlan:
    if spec.aggregations or spec.group_by is not None:
        return _unplan(
            "Listing profiles cannot carry aggregations or grouping; use the "
            "AGGREGATE intent."
        )

    clauses, params, unsupported = _profile_filters(spec)
    if unsupported:
        return _unplan(" ".join(unsupported))

    limit = _limit_value(spec)
    params["limit"] = limit
    level_column = (
        "pressure_adjusted"
        if spec.values is MeasurementValues.ADJUSTED
        else "pressure"
    )
    sql = (
        "SELECT p.profile_id, f.wmo_number, p.cycle_number, p.profile_time, "
        "p.latitude, p.longitude, p.data_mode, "
        f"JSON_LENGTH(COALESCE(p.{level_column}, '[]')) AS level_count "
        "FROM argo_profiles p "
        "JOIN argo_floats f ON p.float_id = f.float_id "
        f"{_where(clauses)} "
        f"ORDER BY {_order_clause(spec)} "
        "LIMIT :limit"
    )
    return QueryPlan(
        operation=PlanOperation.LIST_PROFILES,
        template_id="list_profiles",
        supported=True,
        sql=sql,
        params=params,
        limit=limit,
        result_shape=ResultShape.PROFILE_ROWS,
    )


def _plan_profile_detail(spec: QuerySpec) -> QueryPlan:
    if not spec.wmo_numbers and not spec.cycle_numbers:
        return _unplan(
            "Profile detail requires a WMO number or a cycle number; QuerySpec "
            "has no profile-id field, so no arbitrary first row is selected."
        )
    if spec.aggregations or spec.group_by is not None:
        return _unplan("Profile detail cannot carry aggregations or grouping.")

    clauses, params, unsupported = _profile_filters(spec)
    if unsupported:
        return _unplan(" ".join(unsupported))

    if spec.values is MeasurementValues.ADJUSTED:
        measurement_columns = (
            "p.pressure_adjusted, p.pressure_adjusted_qc, "
            "p.temperature_adjusted, p.temperature_adjusted_qc, "
            "p.salinity_adjusted, p.salinity_adjusted_qc, "
            "p.bgc_adjusted, p.bgc_adjusted_qc"
        )
    else:
        measurement_columns = (
            "p.pressure, p.pressure_qc, p.temperature, p.temperature_qc, "
            "p.salinity, p.salinity_qc, p.bgc_params, p.bgc_qc"
        )

    limit = _limit_value(spec, default=1)
    params["limit"] = limit
    sql = (
        "SELECT p.profile_id, f.wmo_number, p.cycle_number, p.profile_time, "
        "p.latitude, p.longitude, p.data_mode, "
        "p.profile_pres_qc, p.profile_temp_qc, p.profile_psal_qc, "
        "p.parameter_data_mode, p.source_file, p.source_file_sha256, "
        f"{measurement_columns} "
        "FROM argo_profiles p "
        "JOIN argo_floats f ON p.float_id = f.float_id "
        f"{_where(clauses)} "
        f"ORDER BY {_order_clause(spec)} "
        "LIMIT :limit"
    )
    return QueryPlan(
        operation=PlanOperation.PROFILE_DETAIL,
        template_id="profile_detail",
        supported=True,
        sql=sql,
        params=params,
        limit=limit,
        result_shape=ResultShape.PROFILE_DETAIL,
    )


def _plan_parameter_availability(spec: QuerySpec) -> QueryPlan:
    if not spec.parameters:
        return _unplan("Parameter availability requires a parameter.")
    if spec.aggregations or spec.group_by is not None:
        return _unplan("Parameter availability cannot carry aggregations.")

    clauses, params, unsupported = _profile_filters(spec)
    if unsupported:
        return _unplan(" ".join(unsupported))

    selects = []
    for parameter in spec.parameters:
        core = _CORE_PARAMETER_COLUMNS[spec.values].get(parameter)
        if core is not None:
            selects.append(
                f"JSON_LENGTH(COALESCE(p.{core}, '[]')) > 0 "
                f"AS has_{parameter.value}"
            )
            continue
        key = _BGC_PARAMETER_KEYS[parameter]
        source = "bgc_adjusted" if spec.values is MeasurementValues.ADJUSTED else "bgc_params"
        selects.append(
            "JSON_CONTAINS_PATH(COALESCE("
            f"p.{source}, '{{}}'), 'one', '$.{key}') "
            f"AS has_{parameter.value}"
        )

    limit = _limit_value(spec)
    params["limit"] = limit
    sql = (
        "SELECT p.profile_id, f.wmo_number, p.cycle_number, p.profile_time, "
        + ", ".join(selects)
        + " FROM argo_profiles p "
        "JOIN argo_floats f ON p.float_id = f.float_id "
        f"{_where(clauses)} "
        f"ORDER BY {_order_clause(spec)} "
        "LIMIT :limit"
    )
    return QueryPlan(
        operation=PlanOperation.PARAMETER_AVAILABILITY,
        template_id="parameter_availability",
        supported=True,
        sql=sql,
        params=params,
        limit=limit,
        result_shape=ResultShape.AVAILABILITY_ROWS,
    )


def _plan_metadata_info(spec: QuerySpec) -> QueryPlan:
    if spec.sort.field not in (SortField.PROFILE_TIME, SortField.WMO_NUMBER):
        return _unplan(
            "Float metadata supports only WMO ordering in Phase 3C-D."
        )
    if (
        spec.parameters
        or spec.cycle_numbers
        or spec.time_range
        or spec.bounding_box
        or spec.pressure_range
        or spec.depth_requested
        or spec.depth_range_m
        or spec.qc_filter
        or spec.data_mode
        or spec.aggregations
        or spec.group_by
    ):
        return _unplan(
            "Metadata info supports only the WMO filter in Phase 3C-D."
        )

    clauses: list[str] = []
    params: dict = {}
    if spec.wmo_numbers:
        clauses.append(
            _in_clause("f.wmo_number", spec.wmo_numbers, "wmo", params)
        )
    limit = _limit_value(spec)
    params["limit"] = limit
    sql = (
        "SELECT f.wmo_number, f.project_name, f.platform_type, "
        "COUNT(p.profile_id) AS profile_count "
        "FROM argo_floats f "
        "LEFT JOIN argo_profiles p ON f.float_id = p.float_id "
        f"{_where(clauses)} "
        "GROUP BY f.float_id, f.wmo_number, f.project_name, f.platform_type "
        "ORDER BY f.wmo_number ASC "
        "LIMIT :limit"
    )
    return QueryPlan(
        operation=PlanOperation.METADATA_INFO,
        template_id="metadata_info",
        supported=True,
        sql=sql,
        params=params,
        limit=limit,
        result_shape=ResultShape.FLOAT_METADATA,
    )


def _plan_aggregate(spec: QuerySpec) -> QueryPlan:
    if any(operation is not Aggregation.COUNT for operation in spec.aggregations):
        return _unplan(
            "MEAN, MIN and MAX require per-level JSON post-processing, which "
            "Phase 3C-D does not implement; only COUNT is plannable."
        )

    clauses, params, unsupported = _profile_filters(spec)
    if unsupported:
        return _unplan(" ".join(unsupported))

    if spec.group_by is not None:
        group_expressions = {
            GroupBy.YEAR: "YEAR(p.profile_time)",
            GroupBy.MONTH: "MONTH(p.profile_time)",
            GroupBy.WMO: "f.wmo_number",
            GroupBy.PROFILE: "p.profile_id",
        }
        group_expression = group_expressions[spec.group_by]
        limit = _limit_value(spec)
        params["limit"] = limit
        sql = (
            f"SELECT {group_expression} AS group_key, "
            "COUNT(*) AS profile_count "
            "FROM argo_profiles p "
            "JOIN argo_floats f ON p.float_id = f.float_id "
            f"{_where(clauses)} "
            f"GROUP BY {group_expression} "
            "ORDER BY group_key ASC "
            "LIMIT :limit"
        )
        return QueryPlan(
            operation=PlanOperation.COUNT_PROFILES,
            template_id="count_profiles",
            supported=True,
            sql=sql,
            params=params,
            limit=limit,
            scan_limit=MAX_AGGREGATE_PROFILE_SCAN,
            result_shape=ResultShape.COUNT_BY_GROUP,
        )

    sql = (
        "SELECT COUNT(*) AS profile_count "
        "FROM argo_profiles p "
        "JOIN argo_floats f ON p.float_id = f.float_id "
        f"{_where(clauses)}"
    )
    return QueryPlan(
        operation=PlanOperation.COUNT_PROFILES,
        template_id="count_profiles",
        supported=True,
        sql=sql,
        params=params,
        scan_limit=MAX_AGGREGATE_PROFILE_SCAN,
        result_shape=ResultShape.COUNT,
    )


# ---------------------------------------------------------------------------
# Execution boundary
# ---------------------------------------------------------------------------


def execute_spec(spec: QuerySpec, engine, capabilities=None):
    """Validate, plan and execute one QuerySpec through an existing engine.

    Execution accepts a specification, never SQL: the statement is regenerated
    by this module, so no caller can submit an arbitrary SQL string.  Planning
    and execution remain separate functions; the returned plan is inspectable
    without running it.
    """

    return _execute_plan(plan_query(spec, capabilities), engine)


def _execute_plan(plan: QueryPlan, engine):
    if not plan.supported:
        raise ValueError("Cannot execute an unsupported query plan.")
    if plan.template_id not in TEMPLATE_IDS:
        raise ValueError("Unknown query-plan template.")
    if plan.sql is None:
        return {"rows": [], "result_shape": plan.result_shape.value}

    from sqlalchemy import text

    with engine.connect() as connection:
        rows = (
            connection.execute(text(plan.sql), plan.params)
            .mappings()
            .fetchall()
        )

    return {
        "rows": [dict(row) for row in rows],
        "result_shape": plan.result_shape.value,
        "limit": plan.limit,
    }
