"""Validated BGC-Argo scientific calculation layer (Phase 3A).

All calculations operate exclusively on validated BGC measurements retrieved
from MySQL (``bgc_params``, ``bgc_adjusted``, ``bgc_qc``, ``bgc_adjusted_qc``,
``bgc_pressure``, ``bgc_source_metadata``).

Scientific integrity guarantees:
- BGC measurements are paired ONLY with ``bgc_pressure``, never core
  ``pressure``.
- Missing values (``None``), ``NaN``, ``inf``, and NetCDF fill values
  (e.g. ``99999.0``) are never treated as valid observations or zero.
- QC filtering is explicit and parameter-aware; excluded counts and the
  applied QC policy are always reported.
- Raw measurements are never mutated.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from query_spec import GOOD_MEASUREMENT_QC_FLAGS, Parameter

# Canonical units for BGC parameters in IntelliQuery.
BGC_PARAMETER_UNITS: dict[str, str] = {
    Parameter.DOXY.value: "micromole/kg",
    Parameter.CHLA.value: "mg/m3",
    Parameter.CHLA_FLUORESCENCE.value: "mg/m3",
    Parameter.BBP700.value: "m-1",
    Parameter.NITRATE.value: "micromole/kg",
    Parameter.PH_IN_SITU_TOTAL.value: "total pH scale",
}

BGC_PARAMETER_DESCRIPTIONS: dict[str, str] = {
    Parameter.DOXY.value: "Dissolved oxygen",
    Parameter.CHLA.value: "Chlorophyll-a",
    Parameter.CHLA_FLUORESCENCE.value: "Chlorophyll-a fluorescence",
    Parameter.BBP700.value: "Particle backscattering at 700 nm",
    Parameter.NITRATE.value: "Nitrate",
    Parameter.PH_IN_SITU_TOTAL.value: "In-situ total pH",
}

# Threshold above which ARGO/NetCDF fill values (e.g. 99999.0, 60000.0) are
# treated as missing if any escaped ingestion normalization.
_FILL_VALUE_ABS_LIMIT = 99990.0

QC_POLICY_ALL_FINITE = "all_finite_with_qc_reported"
QC_POLICY_GOOD_ONLY = "argo_good_qc_1_2"


@dataclass(frozen=True)
class BgcCalculationResult:
    """Structured scientific calculation result for a BGC parameter."""

    parameter: str
    unit: str
    aggregation_type: str
    aggregation_scope: str
    profile_id: int | None = None
    wmo_number: int | None = None
    cycle_number: int | None = None
    direction: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    median: float | None = None
    std_dev: float | None = None
    valid_count: int = 0
    missing_count: int = 0
    excluded_count: int = 0
    qc_policy: str = QC_POLICY_ALL_FINITE
    data_mode: str = "raw"
    provenance: str = "mysql.argo_profiles.bgc_params"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "unit": self.unit,
            "profile_id": self.profile_id,
            "wmo_number": self.wmo_number,
            "cycle_number": self.cycle_number,
            "direction": self.direction,
            "aggregation_type": self.aggregation_type,
            "aggregation_scope": self.aggregation_scope,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "median": self.median,
            "std_dev": self.std_dev,
            "valid_count": self.valid_count,
            "missing_count": self.missing_count,
            "excluded_count": self.excluded_count,
            "qc_policy": self.qc_policy,
            "data_mode": self.data_mode,
            "provenance": self.provenance,
            "warnings": list(self.warnings),
        }


def _decode_json_field(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return None


def _is_valid_number(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(f):
        return False
    if abs(f) >= _FILL_VALUE_ABS_LIMIT:
        return False
    return True


def _round_stat(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def extract_valid_bgc_series(
    row: dict[str, Any],
    parameter: str,
    *,
    values_mode: str = "raw",
    qc_policy: str = QC_POLICY_ALL_FINITE,
    accepted_qc_flags: frozenset[str] = GOOD_MEASUREMENT_QC_FLAGS,
) -> tuple[list[float], list[float | None], dict[str, int], list[str]]:
    """Extract valid BGC observations and index-aligned ``bgc_pressure`` levels.

    Never reads core ``pressure``. Returns:
    ``(valid_values, valid_pressures, counts, warnings)``
    where ``counts`` has keys ``valid``, ``missing``, ``excluded_qc``, ``total``.
    """
    warnings: list[str] = []
    param_key = parameter.value if isinstance(parameter, Parameter) else str(parameter)

    if values_mode == "adjusted":
        val_container = _decode_json_field(row.get("bgc_adjusted"))
        qc_container = _decode_json_field(row.get("bgc_adjusted_qc"))
    else:
        val_container = _decode_json_field(row.get("bgc_params"))
        qc_container = _decode_json_field(row.get("bgc_qc"))

    if not isinstance(val_container, dict) or param_key not in val_container:
        return [], [], {"valid": 0, "missing": 0, "excluded_qc": 0, "total": 0}, warnings

    raw_values = val_container.get(param_key)
    if not isinstance(raw_values, list):
        return [], [], {"valid": 0, "missing": 0, "excluded_qc": 0, "total": 0}, warnings

    raw_qc = (
        qc_container.get(param_key)
        if isinstance(qc_container, dict)
        else None
    )
    has_qc_array = isinstance(raw_qc, list) and len(raw_qc) == len(raw_values)
    any_qc_flag = (
        has_qc_array and any(flag is not None and str(flag).strip() != "" for flag in raw_qc)
    )

    if not any_qc_flag:
        warnings.append(
            f"QC flags unavailable or unflagged for {param_key}; "
            "all finite numeric values were evaluated."
        )

    bgc_pressure = _decode_json_field(row.get("bgc_pressure"))
    has_bgc_pressure = (
        isinstance(bgc_pressure, list) and len(bgc_pressure) == len(raw_values)
    )
    if not has_bgc_pressure:
        warnings.append(
            f"bgc_pressure unavailable or length-mismatched for {param_key}; "
            "core pressure was not substituted."
        )

    src_meta = _decode_json_field(row.get("bgc_source_metadata"))
    if isinstance(src_meta, dict) and src_meta.get("grid_check") == "pres_scale_suspect":
        warnings.append(
            "Source BGC metadata flagged grid_check=pres_scale_suspect; "
            "BGC pressure scale may require verification."
        )

    valid_values: list[float] = []
    valid_pressures: list[float | None] = []
    missing_count = 0
    excluded_qc_count = 0

    for idx, raw_val in enumerate(raw_values):
        if not _is_valid_number(raw_val):
            missing_count += 1
            continue

        if qc_policy == QC_POLICY_GOOD_ONLY and any_qc_flag:
            flag = raw_qc[idx]
            flag_str = str(flag).strip() if flag is not None else ""
            if flag_str not in accepted_qc_flags:
                excluded_qc_count += 1
                continue

        valid_values.append(float(raw_val))
        if has_bgc_pressure:
            p_val = bgc_pressure[idx]
            valid_pressures.append(float(p_val) if _is_valid_number(p_val) else None)
        else:
            valid_pressures.append(None)

    counts = {
        "valid": len(valid_values),
        "missing": missing_count,
        "excluded_qc": excluded_qc_count,
        "total": len(raw_values),
    }
    return valid_values, valid_pressures, counts, warnings


def calculate_bgc_statistics(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    parameter: str | Parameter,
    *,
    scope: str = "result_set",
    aggregation_type: str = "summary",
    values_mode: str = "raw",
    qc_policy: str = QC_POLICY_ALL_FINITE,
) -> BgcCalculationResult:
    """Compute validated descriptive statistics across one or more SQL rows."""
    param_key = parameter.value if isinstance(parameter, Parameter) else str(parameter)
    unit = BGC_PARAMETER_UNITS.get(param_key, "unknown")
    provenance = (
        "mysql.argo_profiles.bgc_adjusted"
        if values_mode == "adjusted"
        else "mysql.argo_profiles.bgc_params"
    )

    all_values: list[float] = []
    total_missing = 0
    total_excluded = 0
    warning_set: list[str] = []

    for row in rows:
        vals, _, counts, row_warnings = extract_valid_bgc_series(
            row,
            param_key,
            values_mode=values_mode,
            qc_policy=qc_policy,
        )
        all_values.extend(vals)
        total_missing += counts["missing"]
        total_excluded += counts["excluded_qc"]
        for w in row_warnings:
            if w not in warning_set:
                warning_set.append(w)

    if len(rows) > 1 and scope == "result_set":
        warning_set.append(
            f"Aggregated across {len(rows)} profiles; values are not grouped by depth."
        )

    profile_id = rows[0].get("profile_id") if len(rows) == 1 else None
    wmo_set = {r.get("wmo_number") for r in rows if r.get("wmo_number") is not None}
    wmo_number = next(iter(wmo_set)) if len(wmo_set) == 1 else None
    cycle_number = rows[0].get("cycle_number") if len(rows) == 1 else None
    dir_set = {r.get("direction") for r in rows if r.get("direction")}
    direction = next(iter(dir_set)) if len(dir_set) == 1 else None

    if not all_values:
        warning_set.append(
            f"No valid {param_key} measurements found in the selected records."
        )
        return BgcCalculationResult(
            parameter=param_key,
            unit=unit,
            profile_id=profile_id,
            wmo_number=wmo_number,
            cycle_number=cycle_number,
            direction=direction,
            aggregation_type=aggregation_type,
            aggregation_scope=scope,
            valid_count=0,
            missing_count=total_missing,
            excluded_count=total_excluded,
            qc_policy=qc_policy,
            data_mode=values_mode,
            provenance=provenance,
            warnings=tuple(warning_set),
        )

    minimum = _round_stat(min(all_values))
    maximum = _round_stat(max(all_values))
    mean = _round_stat(statistics.fmean(all_values))
    median = _round_stat(statistics.median(all_values))
    std_dev = _round_stat(statistics.stdev(all_values)) if len(all_values) >= 2 else None
    if len(all_values) < 2:
        warning_set.append("Standard deviation requires at least 2 valid observations.")

    return BgcCalculationResult(
        parameter=param_key,
        unit=unit,
        profile_id=profile_id,
        wmo_number=wmo_number,
        cycle_number=cycle_number,
        direction=direction,
        aggregation_type=aggregation_type,
        aggregation_scope=scope,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        median=median,
        std_dev=std_dev,
        valid_count=len(all_values),
        missing_count=total_missing,
        excluded_count=total_excluded,
        qc_policy=qc_policy,
        data_mode=values_mode,
        provenance=provenance,
        warnings=tuple(warning_set),
    )


def _matches_operator(
    val: float, operator: str, threshold: float, upper_threshold: float | None = None
) -> bool:
    if operator == ">":
        return val > threshold
    if operator == "<":
        return val < threshold
    if operator == ">=":
        return val >= threshold
    if operator == "<=":
        return val <= threshold
    if operator == "=":
        return math.isclose(val, threshold, rel_tol=1e-9, abs_tol=1e-9)
    if operator == "between":
        if upper_threshold is None:
            return False
        return threshold <= val <= upper_threshold
    return False


def filter_rows_by_bgc_threshold(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    parameter: str | Parameter,
    operator: str,
    threshold: float,
    upper_threshold: float | None = None,
    *,
    scope: str = "measurement",
    aggregation: str | None = None,
    values_mode: str = "raw",
    qc_policy: str = QC_POLICY_ALL_FINITE,
) -> list[dict[str, Any]]:
    """Filter SQL rows by a validated BGC numeric condition.

    Distinguishes:
    - ``scope="measurement"``: at least one valid measurement satisfies the
      comparison.
    - ``scope="profile"``: the profile-level statistic (default ``mean``)
      satisfies the comparison.
    - ``scope="float"``: groups rows by ``wmo_number``, computes the float-level
      statistic (default ``max`` or requested ``aggregation``), and returns
      qualifying float summary dicts.
    """
    param_key = parameter.value if isinstance(parameter, Parameter) else str(parameter)

    if scope == "float":
        by_wmo: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            wmo = row.get("wmo_number")
            if wmo is not None:
                by_wmo.setdefault(wmo, []).append(row)

        agg_name = (aggregation or "MAX").upper()
        matched_floats: list[dict[str, Any]] = []
        for wmo in sorted(by_wmo):
            calc = calculate_bgc_statistics(
                by_wmo[wmo],
                param_key,
                scope="float",
                aggregation_type=agg_name,
                values_mode=values_mode,
                qc_policy=qc_policy,
            )
            if calc.valid_count == 0:
                continue
            metric = _select_metric(calc, agg_name)
            if metric is not None and _matches_operator(
                metric, operator, threshold, upper_threshold
            ):
                matched_floats.append(
                    {
                        "wmo_number": wmo,
                        "profile_count": len(by_wmo[wmo]),
                        "metric": metric,
                        "aggregation": agg_name,
                        "parameter": param_key,
                        "unit": calc.unit,
                        "valid_count": calc.valid_count,
                    }
                )
        return matched_floats

    matched_rows: list[dict[str, Any]] = []
    for row in rows:
        vals, _, counts, _ = extract_valid_bgc_series(
            row,
            param_key,
            values_mode=values_mode,
            qc_policy=qc_policy,
        )
        if not vals:
            continue

        if scope == "profile":
            agg_name = (aggregation or "MEAN").upper()
            calc = calculate_bgc_statistics(
                [row],
                param_key,
                scope="profile",
                aggregation_type=agg_name,
                values_mode=values_mode,
                qc_policy=qc_policy,
            )
            metric = _select_metric(calc, agg_name)
            if metric is not None and _matches_operator(
                metric, operator, threshold, upper_threshold
            ):
                enriched = dict(row)
                enriched["_bgc_metric"] = metric
                enriched["_bgc_valid_count"] = counts["valid"]
                matched_rows.append(enriched)
        else:
            # Measurement-level: at least one valid observation matches.
            matching_vals = [
                v for v in vals
                if _matches_operator(v, operator, threshold, upper_threshold)
            ]
            if matching_vals:
                enriched = dict(row)
                enriched["_bgc_matching_count"] = len(matching_vals)
                enriched["_bgc_valid_count"] = counts["valid"]
                matched_rows.append(enriched)

    return matched_rows


def _select_metric(calc: BgcCalculationResult, agg_name: str) -> float | None:
    if agg_name == "MIN":
        return calc.minimum
    if agg_name == "MAX":
        return calc.maximum
    if agg_name == "MEDIAN":
        return calc.median
    if agg_name == "STDDEV":
        return calc.std_dev
    return calc.mean
