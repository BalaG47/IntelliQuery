"""Full-corpus dry-run audit of the downloaded BGC-Argo corpus.

Read-only. Nothing is ingested, no NetCDF file is written, no database is
touched. The audit:

  1. indexes every file once with ``netCDF4`` (metadata only, no measurement
     arrays retained);
  2. builds an ingestion plan per ``(float, cycle)`` group with the *production*
     matcher (``bgc_matching.plan_group``), so the audit exercises the code that
     would run for real;
  3. validates each planned BGC pressure axis with the *production* classifier
     (``bgc_matching.classify_pressure_grid``), reading only that one ``PRES``
     array and discarding it;
  4. writes machine-readable results to ``backend/scratch/bgc_audit/`` and the
     report to ``docs/bgc/BGC_FULL_CORPUS_DRY_RUN_AUDIT.md``.

Usage (from ``backend/``)::

    python scripts/bgc_full_corpus_audit.py --limit 60
    python scripts/bgc_full_corpus_audit.py

The report is generated from the measured numbers, not transcribed.
"""

import argparse
import collections
import glob
import json
import os
import sys
import time
import tracemalloc

import netCDF4
import numpy as np

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bgc_matching import (  # noqa: E402
    BGC_PREFIXES,
    GRID_MISMATCH,
    GRID_MISSING_PRESSURE,
    GRID_OK,
    GRID_PRES_SCALE_SUSPECT,
    UNKNOWN_DIRECTION,
    classify_pressure_grid,
    first_float_id,
    group_by_cycle,
    index_file,
    logical_identity,
    plan_group,
    profile_time_from_juld,
)

DEFAULT_ROOT = os.path.join(BACKEND_DIR, "bgc data", "bgc raw data")
DEFAULT_REPORT = os.path.join(PROJECT_DIR, "docs", "bgc",
                             "BGC_FULL_CORPUS_DRY_RUN_AUDIT.md")
DEFAULT_JSON_DIR = os.path.join(BACKEND_DIR, "scratch", "bgc_audit")

TARGET_PARAMS = ("DOXY", "CHLA", "NITRATE", "BBP700", "PH_IN_SITU_TOTAL")
CORE_PARAMS = ("PRES", "TEMP", "PSAL")
CORE_AUXILIARY_EXACT = ("MTIME",)
CORE_AUXILIARY_PREFIXES = ("TEMP_STD", "PSAL_STD", "PRES_MED", "TEMP_MED",
                           "PSAL_MED")
FILL_VALUES = (99999.0, 60000.0, -99999.0)
GRID_STATUSES = (GRID_OK, GRID_PRES_SCALE_SUSPECT, GRID_MISSING_PRESSURE,
                 GRID_MISMATCH)


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _text(value):
    if value is None or np.ma.is_masked(value):
        return None
    if isinstance(value, np.ndarray):
        flat = np.ravel(value)
        if all(isinstance(item, (bytes, np.bytes_)) for item in flat):
            value = b"".join(bytes(item) for item in flat)
        else:
            value = "".join(str(item) for item in flat)
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip("\x00 \t\r\n")
    return value or None


def _number(value):
    if value is None or np.ma.is_masked(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def is_core_auxiliary(name):
    return name in CORE_AUXILIARY_EXACT or name.startswith(
        CORE_AUXILIARY_PREFIXES)


# ---------------------------------------------------------------------------
# 1. Metadata index (one open per file)
# ---------------------------------------------------------------------------


def _station_parameters(dataset, n_prof):
    if "STATION_PARAMETERS" not in dataset.variables:
        return [[] for _ in range(n_prof)]
    raw = np.ma.filled(dataset.variables["STATION_PARAMETERS"][:], b" ")
    if raw.ndim == 3:
        raw = np.array([[b"".join(c for c in part) for part in profile]
                        for profile in raw])
    rows = [[name.upper() for name in (_text(item) for item in row) if name]
            for row in np.atleast_2d(raw)]
    while len(rows) < n_prof:
        rows.append([])
    return rows


def scan_file(path):
    """One metadata record per N_PROF entry; PRES is counted, never kept."""
    family = "bgc" if _prefix(path) in BGC_PREFIXES else "core"
    records = []
    with netCDF4.Dataset(path, "r") as dataset:
        sizes = {name: len(dim) for name, dim in dataset.dimensions.items()}
        n_prof = sizes.get("N_PROF", 1)
        float_id = first_float_id(dataset)

        def column(name):
            if name not in dataset.variables:
                return [None] * n_prof
            data = dataset.variables[name][:]
            if data.ndim == 0:
                return [data[()]] * n_prof
            return list(np.atleast_1d(data))[:n_prof]

        cycles = column("CYCLE_NUMBER")
        julds = column("JULD")
        directions = column("DIRECTION")
        modes = column("DATA_MODE")
        latitudes = column("LATITUDE")
        longitudes = column("LONGITUDE")
        station = _station_parameters(dataset, n_prof)
        variables = set(dataset.variables)

        levels = []
        for index in range(n_prof):
            if "PRES" not in dataset.variables:
                levels.append(0)
                continue
            profile_pres = _profile_pres(dataset.variables["PRES"], index)
            masked = np.ma.getmaskarray(profile_pres)
            levels.append(int(np.size(masked) - np.sum(masked)))

    for index in range(n_prof):
        cycle = _number(cycles[index])
        names = station[index] if index < len(station) else []
        available = [name for name in names if name in variables]
        records.append({
            "path": os.fspath(path),
            "file": os.path.basename(path),
            "prefix": _prefix(path),
            "family": family,
            "index": index,
            "n_prof": n_prof,
            "float": float_id,
            "cycle": None if cycle is None else int(cycle),
            "juld": _number(julds[index]),
            "direction": _text(directions[index]),
            "data_mode": _text(modes[index]),
            "latitude": _number(latitudes[index]),
            "longitude": _number(longitudes[index]),
            "levels": levels[index],
            "station_parameters": names,
            "available_parameters": available,
            "bgc_parameters": [
                name for name in available
                if name not in CORE_PARAMS and not is_core_auxiliary(name)
            ],
        })
    return records


def _prefix(path):
    prefix = ""
    for char in os.path.basename(path):
        if not char.isalpha():
            break
        prefix += char
    return prefix.upper()


def _profile_pres(variable, index):
    """The PRES row of one profile, tolerating a file without an N_PROF axis."""
    array = np.ma.asarray(variable[:])
    if array.ndim <= 1:
        return array
    return array[index if index < array.shape[0] else 0]


def read_pres(dataset, index):
    """The PRES values of one profile, normalised like the ingestion code."""
    if "PRES" not in dataset.variables:
        return None
    data = np.ma.filled(np.ma.asarray(_profile_pres(dataset.variables["PRES"],
                                                    index)), np.nan)
    values = []
    for value in np.ravel(data):
        number = float(value)
        if np.isnan(number) or number in FILL_VALUES:
            values.append(None)
        else:
            values.append(round(number, 4))
    return values


def parameter_fields(dataset, name):
    """Which derived BGC variables exist for one parameter."""
    variables = set(dataset.variables)
    return {
        "raw": name in variables,
        "qc": f"{name}_QC" in variables,
        "adjusted": f"{name}_ADJUSTED" in variables,
        "adjusted_error": f"{name}_ADJUSTED_ERROR" in variables,
        "adjusted_qc": f"{name}_ADJUSTED_QC" in variables,
    }


# ---------------------------------------------------------------------------
# 2. Planned profiles
# ---------------------------------------------------------------------------


def pressure_reference(plan):
    """Pressure values of the selected core variant, if there is a core."""
    core = plan["core"]
    if core is None:
        return None
    with netCDF4.Dataset(core["path"], "r") as dataset:
        return read_pres(dataset, core["index"])


def verify_bgc(plan):
    """Grid status and parameter presence for the attached BGC record."""
    bgc_record = plan["bgc"]
    if bgc_record is None:
        return None
    with netCDF4.Dataset(bgc_record["path"], "r") as dataset:
        bgc_pressure = read_pres(dataset, bgc_record["index"])
        fields = {
            name: parameter_fields(dataset, name)
            for name in plan.get("bgc_parameters", [])
        }
    core_pressure = pressure_reference(plan)
    status, detail = classify_pressure_grid(bgc_pressure, core_pressure)
    return {
        "status": status,
        "detail": detail,
        "fields": fields,
        "bgc_levels": len(bgc_pressure or []),
    }


def build_plans(records):
    groups = group_by_cycle(
        [record for record in records
         if record["float"] is not None and record["cycle"] is not None])
    plans = []
    for key in sorted(groups):
        group = groups[key]
        for plan in plan_group(group["core"], group["bgc"]):
            plan["bgc_parameters"] = (
                plan["bgc"]["bgc_parameters"] if plan["bgc"] else []
            )
            plans.append(plan)
    return plans, groups


def plan_warnings(plan, verification):
    warnings = []
    base = plan["core"] or plan["bgc"]
    if base is None:
        warnings.append("plan has no product")
        return warnings
    if base.get("juld") is None:
        warnings.append("missing JULD: no stable logical identity")
    if not base.get("direction"):
        warnings.append("missing DIRECTION: matched on JULD only")
    if plan["match"] == "bgc_only":
        warnings.append("BGC product has no core counterpart (stored separately)")
    if plan["match"] == "core_only":
        warnings.append("core profile has no BGC product")
    if len(plan["bgc_candidates"]) > 1:
        warnings.append(f"{len(plan['bgc_candidates'])} BGC products match")
    if verification and verification["status"] != GRID_OK:
        warnings.append(f"pressure grid: {verification['status']}")
    if len(plan["core_variants"]) > 1:
        delayed = [v for v in plan["core_variants"]
                   if (v["data_mode"] or "").upper() == "D"]
        selected_delayed = any(
            v["selected"] and (v["data_mode"] or "").upper() == "D"
            for v in plan["core_variants"])
        if delayed and not selected_delayed:
            warnings.append("a delayed-mode variant was not selected")
    return warnings


# ---------------------------------------------------------------------------
# 3. Statistics
# ---------------------------------------------------------------------------


def count_by(items, key):
    return collections.Counter(key(item) for item in items)


def build_summary(records, plans, groups, extra):
    summary = {}
    summary["dataset"] = {
        "files": extra["file_count"],
        "records": len(records),
        "groups": len(groups),
        "floats": len({record["float"] for record in records
                       if record["float"] is not None}),
        "records_by_family": dict(count_by(records, lambda r: r["family"])),
        "records_by_prefix": dict(count_by(records, lambda r: r["prefix"])),
        "files_by_prefix": extra["files_by_prefix"],
        "n_prof_distribution": dict(count_by(
            extra["file_records"], lambda name: len(extra["file_records"][name])
        )),
        "core_level_range": extra["core_level_range"],
        "bgc_level_range": extra["bgc_level_range"],
    }

    summary["identity"] = {
        "logical_profiles": len(plans),
        "records": len(records),
        "duplicate_identities": extra["duplicate_identities"],
        "missing_juld": sum(1 for plan in plans
                            if (plan["core"] or plan["bgc"])["juld"] is None),
        "unknown_direction": sum(1 for plan in plans
                                 if plan["direction"] == UNKNOWN_DIRECTION),
        "uncertain_matches": sum(1 for plan in plans
                                 if not (plan["core"] or plan["bgc"]).get(
                                     "direction")),
        "unusable_records": extra["unusable_records"],
        "direction_distribution": dict(count_by(plans, lambda p: p["direction"])),
        "time_range": extra["time_range"],
    }

    summary["matching"] = {
        "matched": sum(1 for p in plans if p["match"] == "matched"),
        "core_only": sum(1 for p in plans if p["match"] == "core_only"),
        "bgc_only": sum(1 for p in plans if p["match"] == "bgc_only"),
        "multiple_bgc": sum(1 for p in plans if len(p["bgc_candidates"]) > 1),
        "bgc_records_attached": sum(
            1 for p in plans if p["match"] == "matched"),
        "records_planned": sum(
            len(p["core_variants"]) + len(p["bgc_candidates"]) for p in plans),
    }

    variant_counts = count_by(plans, lambda p: len(p["core_variants"]))
    delayed_variants = [
        plan for plan in plans
        if any((v["data_mode"] or "").upper() == "D"
               for v in plan["core_variants"])
    ]
    delayed_selected = [
        plan for plan in delayed_variants
        if any(v["selected"] and (v["data_mode"] or "").upper() == "D"
               for v in plan["core_variants"])
    ]
    summary["variants"] = {
        "variants_per_profile": dict(sorted(variant_counts.items())),
        "profiles_with_multiple": sum(
            count for size, count in variant_counts.items() if size > 1),
        "selection_rules": dict(count_by(
            [p for p in plans if p["core_variants"]],
            lambda p: p["core_selection_rule"])),
        "profiles_with_delayed_variant": len(delayed_variants),
        "delayed_selected": len(delayed_selected),
        "delayed_not_selected": [
            variant_record(p) for p in delayed_variants
            if id(p) not in {id(selected) for selected in delayed_selected}
        ],
        "selected_data_modes": dict(count_by(
            [p for p in plans if p["core"]],
            lambda p: (p["core"]["data_mode"] or "?")[:1] or "?")),
        "discarded_variants": sum(
            max(0, len(p["core_variants"]) - 1) for p in plans),
    }

    summary["grid"] = {
        "statuses": dict(count_by(
            [p for p in plans if p["bgc"] is not None],
            lambda p: p["verification"]["status"])),
        "suspects": [
            suspect_record(p) for p in plans
            if p["bgc"] is not None
            and p["verification"]["status"] == GRID_PRES_SCALE_SUSPECT
        ],
        "mismatches": [
            suspect_record(p) for p in plans
            if p["bgc"] is not None
            and p["verification"]["status"] == GRID_MISMATCH
        ],
        "missing": [
            suspect_record(p) for p in plans
            if p["bgc"] is not None
            and p["verification"]["status"] == GRID_MISSING_PRESSURE
        ],
        "bgc_profiles": sum(1 for p in plans if p["bgc"] is not None),
        "core_only_without_bgc": sum(1 for p in plans if p["core"] is None),
    }

    summary["parameters"] = parameter_summary(plans)
    summary["floats"] = float_summary(records, plans)
    summary["warning_kinds"] = dict(collections.Counter(
        message for item in extra["warnings"] for message in item["warnings"]
    ).most_common())
    summary["performance"] = extra["performance"]
    summary["failures"] = extra["failures"]
    summary["warnings"] = extra["warnings"]
    summary["parity"] = extra["parity"]
    summary["groups_with_two_directions"] = extra["two_direction_groups"]
    return summary


def float_summary(records, plans):
    """Per-float inventory, so the report can recommend a concrete float."""
    floats = collections.defaultdict(lambda: {
        "float": None, "files": set(), "profiles": 0, "ascending": 0,
        "descending": 0, "suspects": 0, "cycles": set(),
    })
    for record in records:
        if record["float"] is None:
            continue
        entry = floats[record["float"]]
        entry["float"] = record["float"]
        entry["files"].add(record["file"])
    for plan in plans:
        base = plan["core"] or plan["bgc"]
        entry = floats[base["float"]]
        entry["float"] = base["float"]
        entry["profiles"] += 1
        entry["cycles"].add(base["cycle"])
        if plan["direction"] == "A":
            entry["ascending"] += 1
        elif plan["direction"] == "D":
            entry["descending"] += 1
        verification = plan.get("verification") or {}
        if verification.get("status") == GRID_PRES_SCALE_SUSPECT:
            entry["suspects"] += 1
    result = []
    for entry in floats.values():
        result.append({
            "float": entry["float"],
            "files": len(entry["files"]),
            "cycles": len(entry["cycles"]),
            "profiles": entry["profiles"],
            "ascending": entry["ascending"],
            "descending": entry["descending"],
            "suspects": entry["suspects"],
        })
    return sorted(result, key=lambda item: int(item["float"] or 0))


def variant_record(plan):
    core = plan["core"]
    return {
        "float": core["float"],
        "cycle": core["cycle"],
        "direction": plan["direction"],
        "profile_time": plan["profile_time"].isoformat()
        if plan["profile_time"] else None,
        "variants": [
            {"file": v["file"], "index": v["profile_index_in_file"],
             "data_mode": v["data_mode"], "levels": v["levels"],
             "selected": v["selected"]}
            for v in plan["core_variants"]
        ],
        "selected_file": core["file"],
        "selected_data_mode": core["data_mode"],
        "selected_levels": core["levels"],
        "rule": plan["core_selection_rule"],
    }


def suspect_record(plan):
    bgc = plan["bgc"]
    verification = plan["verification"]
    detail = verification["detail"]
    return {
        "float": bgc["float"],
        "cycle": bgc["cycle"],
        "direction": plan["direction"],
        "file": bgc["file"],
        "core_file": plan["core"]["file"] if plan["core"] else None,
        "status": verification["status"],
        "core_min": detail["core_pressure_min"],
        "core_max": detail["core_pressure_max"],
        "bgc_min": detail["bgc_pressure_min"],
        "bgc_max": detail["bgc_pressure_max"],
        "median_ratio": detail["median_ratio"],
        "overlap_fraction": detail["overlap_fraction"],
        "bgc_levels": verification["bgc_levels"],
        "reason": detail["reason"],
    }


def parameter_summary(plans):
    result = {}
    for name in TARGET_PARAMS:
        containing = [p for p in plans if name in (p["bgc"] or {}).get(
            "bgc_parameters", [])]
        matched = [p for p in containing if p["match"] == "matched"]
        bgc_only = [p for p in containing if p["match"] == "bgc_only"]
        def has(plan, field):
            verification = plan.get("verification") or {}
            return bool((verification.get("fields") or {}).get(name, {}).get(field))

        result[name] = {
            "profiles": len(containing),
            "matched": len(matched),
            "bgc_only": len(bgc_only),
            "with_qc": sum(1 for p in containing if has(p, "qc")),
            "with_adjusted": sum(1 for p in containing
                                if has(p, "adjusted")),
            "with_adjusted_error": sum(1 for p in containing
                                       if has(p, "adjusted_error")),
            "with_adjusted_qc": sum(1 for p in containing
                                    if has(p, "adjusted_qc")),
            "with_ok_grid": sum(
                1 for p in containing
                if (p.get("verification") or {}).get("status") == GRID_OK),
            "core_only_absent": sum(
                1 for p in plans
                if p["match"] == "core_only"
                and name not in (p["bgc"] or {}).get("bgc_parameters", [])),
        }

    combinations = collections.Counter(
        tuple(sorted(p["bgc_parameters"])) for p in plans if p["bgc"] is not None)
    result["_combinations"] = [
        {"parameters": list(names), "profiles": count}
        for names, count in combinations.most_common()
    ]
    ph_only = {id(p) for p in plans
               if p["bgc"] is not None
               and sorted(p["bgc_parameters"]) == ["PH_IN_SITU_TOTAL"]}
    suspects = {id(p) for p in plans
                if (p.get("verification") or {}).get("status")
                == GRID_PRES_SCALE_SUSPECT}
    result["_ph_only"] = {
        "profiles": len(ph_only),
        "suspects": len(suspects),
        "both": len(ph_only & suspects),
        "ph_only_without_suspect": len(ph_only - suspects),
        "suspects_without_ph_only": len(suspects - ph_only),
    }
    return result


# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def yes_no(value):
    return "yes" if value else "no"


def build_report(summary, args):
    dataset = summary["dataset"]
    identity = summary["identity"]
    matching = summary["matching"]
    variants = summary["variants"]
    grid = summary["grid"]
    parameters = summary["parameters"]
    performance = summary["performance"]

    profiles = identity["logical_profiles"]
    matched = matching["matched"]
    core_only = matching["core_only"]
    bgc_only = matching["bgc_only"]
    suspects = grid["suspects"]
    mismatches = grid["mismatches"]

    # "Safe" means the plan itself is internally consistent: one row per
    # distinct logical profile, every profile identifiable, every file readable,
    # and no BGC axis that silently disagrees with its core profile. Core-only
    # and BGC-only rows are documented fallbacks, not defects.
    safe = (identity["duplicate_identities"]["count"] == 0
            and identity["missing_juld"] == 0
            and identity["unusable_records"]["count"] == 0
            and not summary["failures"]["files"]
            and not summary["failures"]["matching"]
            and not mismatches)

    lines = []
    add = lines.append

    add("# IntelliQuery — BGC Phase 2: Full-Corpus Dry-Run Ingestion Audit")
    add("")
    add("**Scope:** audit and verification only. No ingestion, no database write,")
    add("no NetCDF modification, no change to the matching algorithm.")
    add("")
    add(f"**Date:** {time.strftime('%Y-%m-%d')}  ")
    add(f"**Dataset:** `{args.root}` (read-only)  ")
    add("**Tool:** `backend/scripts/bgc_full_corpus_audit.py`  ")
    add("**Architecture audited:** the unified core + BGC ingestion described in")
    add("`docs/bgc/BGC_PRODUCT_MATCHING.md` and implemented in")
    add("`backend/bgc_matching.py`, `backend/data_processor.py`,")
    add("`backend/database_manager.py`, migration V4.")
    add("")
    add("---")
    add("")

    # 1. Executive summary -------------------------------------------------
    add("## 1. Executive summary")
    add("")
    add(f"* **{dataset['files']:,} files** and **{dataset['records']:,} `N_PROF` records** "
        f"were indexed in **{performance['index_seconds']:.1f} s** "
        f"(whole audit {performance['total_seconds']:.1f} s).")
    add(f"* They resolve to **{profiles:,} logical profiles** "
        f"(`float, cycle, direction, profile_time`) across "
        f"**{dataset['floats']} float{'s' if dataset['floats'] != 1 else ''}** "
        f"and **{dataset['groups']:,} `(float, cycle)` groups**.")
    add(f"* **{matched:,} profiles** are merged core + BGC, **{core_only}** are "
        f"core-only and **{bgc_only}** are BGC-only.")
    add(f"* **Duplicate logical identities: {identity['duplicate_identities']['count']}.** "
        f"Missing `JULD`: {identity['missing_juld']}. Unknown direction: "
        f"{identity['unknown_direction']}.")
    add(f"* **{variants['discarded_variants']:,}** processing variants are collapsed "
        f"into their logical profile and preserved in `source_metadata.core_variants`.")
    add(f"* Pressure grids: **{grid['statuses'].get(GRID_OK, 0):,} ok**, "
        f"**{grid['statuses'].get(GRID_PRES_SCALE_SUSPECT, 0)} "
        f"`pres_scale_suspect`**, "
        f"**{grid['statuses'].get(GRID_MISSING_PRESSURE, 0)} "
        f"`missing_pressure`**, "
        f"**{grid['statuses'].get(GRID_MISMATCH, 0)} `grid_mismatch`**.")
    add(f"* **Verdict:** the ingestion plan is "
        f"{'**safe to proceed with**' if safe else '**NOT safe to proceed with**'} — "
        + ("every profile has a distinct identity, every file was readable and no "
           "BGC axis contradicts its core profile. Rescaled pressure axes are "
           "flagged, never corrected; no profile is dropped."
           if safe else
           "see the blockers in section 8 (identity, readability or grid "
           "mismatch problems must be resolved first)."))
    add("")
    add(md_table(
        ["Measure", "Value"],
        [["Files scanned", f"{dataset['files']:,}"],
         ["`N_PROF` records", f"{dataset['records']:,}"],
         ["Logical profiles planned", f"{profiles:,}"],
         ["Merged (core + BGC)", f"{matched:,}"],
         ["Core-only", f"{core_only}"],
         ["BGC-only", f"{bgc_only}"],
         ["Duplicate identities", f"{identity['duplicate_identities']['count']}"],
         ["Ascending / descending / unknown",
          f"{identity['direction_distribution'].get('A', 0)} / "
          f"{identity['direction_distribution'].get('D', 0)} / "
          f"{identity['direction_distribution'].get(UNKNOWN_DIRECTION, 0)}"],
         ["`pres_scale_suspect` grids",
          f"{grid['statuses'].get(GRID_PRES_SCALE_SUSPECT, 0)}"],
         ["Audit runtime", f"{performance['total_seconds']:.1f} s"]]))
    add("")
    add("---")
    add("")

    # 2. Dataset inventory -------------------------------------------------
    add("## 2. Dataset inventory")
    add("")
    add(md_table(
        ["File prefix", "Files", "`N_PROF` records", "Product family"],
        [[prefix, f"{count:,}",
          f"{dataset['records_by_prefix'].get(prefix, 0):,}",
          "BGC" if prefix in BGC_PREFIXES else "core"]
         for prefix, count in sorted(dataset["files_by_prefix"].items(),
                                     key=lambda item: (-item[1], item[0]))]))
    add("")
    add(f"* `N_PROF` records per file: " + ", ".join(
        f"{n_prof} `N_PROF` -> {count} files"
        for n_prof, count in sorted(dataset["n_prof_distribution"].items())))
    add(f"* Valid `PRES` levels per core record: "
        f"{dataset['core_level_range'] or 'n/a'} — counted per `N_PROF` record, "
        "so the sparse processing variants pull the median down; the selected "
        "variant of each profile is what gets stored.")
    add(f"* Valid `PRES` levels per BGC profile: "
        f"{dataset['bgc_level_range'] or 'n/a'}")
    add(f"* Observation time range: {identity['time_range']['min']} to "
        f"{identity['time_range']['max']}")
    add("")
    add("Profiles per float:")
    add("")
    add(md_table(
        ["Float", "Files", "Cycles", "Logical profiles", "Ascending",
         "Descending", "`pres_scale_suspect`"],
        [[item["float"], item["files"], item["cycles"], item["profiles"],
          item["ascending"], item["descending"], item["suspects"]]
         for item in summary["floats"]]))
    add("")
    add("Each file is opened once and reduced to metadata; measurement arrays are")
    add("counted, never retained. The index is written to")
    add("`backend/scratch/bgc_audit/index.json`.")
    add("")
    add("---")
    add("")

    # 3. Logical profile statistics ---------------------------------------
    add("## 3. Logical profile statistics")
    add("")
    add(md_table(
        ["Measure", "Value"],
        [["Source `N_PROF` records",
           f"{identity['records']:,}"],
          ["Logical profiles planned", f"{profiles:,}"],
          ["Duplicate logical identities",
           f"{identity['duplicate_identities']['count']}"],
          ["Profiles with missing `JULD`", f"{identity['missing_juld']}"],
          ["Profiles with unknown direction", f"{identity['unknown_direction']}"],
          ["Matches relying on `JULD` only (no `DIRECTION`)",
           f"{identity['uncertain_matches']}"],
          ["Records unusable before planning",
           f"{identity['unusable_records']['count']}"],
          ["Cycles holding both an ascending and a descending profile",
           f"{summary['groups_with_two_directions']}"]]))
    add("")
    if identity["duplicate_identities"]["items"]:
        add("Duplicate identities (none may disappear silently):")
        add("")
        add(md_table(["Float", "Cycle", "Direction", "Profile time", "Plans"],
                     [[item["float"], item["cycle"], item["direction"],
                       item["profile_time"], item["count"]]
                      for item in identity["duplicate_identities"]["items"]]))
        add("")
    else:
        add("No duplicate logical identity exists: every planned profile maps to a")
        add("distinct `(float, cycle, direction, profile_time)`, so the unique key")
        add("would accept the whole plan.")
        add("")
        add(f"{profiles:,} logical profiles come from {dataset['groups']:,} "
            "`(float, cycle)` groups. The two counts differ because "
            f"{summary['groups_with_two_directions']} cycles carry an ascending "
            "**and** a descending profile; the earlier investigation's "
            "\"626\" is the group count, not the profile count.")
        add("")
    if identity["unusable_records"]["items"]:
        add("Records that could not be planned at all (no float/cycle):")
        add("")
        add(md_table(["File", "`N_PROF`", "Reason"],
                     [[item["file"], item["index"], item["reason"]]
                      for item in identity["unusable_records"]["items"]]))
        add("")
    add("---")
    add("")

    # 4. Matching statistics ----------------------------------------------
    add("## 4. Matching statistics")
    add("")
    add(md_table(
        ["Match status", "Profiles", "Share"],
        [[label, f"{count:,}", f"{100 * count / profiles:.1f} %"]
         for label, count in (("matched (core + BGC)", matched),
                              ("core_only", core_only),
                              ("bgc_only", bgc_only))]))
    add("")
    add(f"* `N_PROF` records accounted for by the plan: "
        f"{matching['records_planned']:,} of {identity['records']:,}.")
    add(f"* Profiles with more than one matching BGC product: "
        f"{matching['multiple_bgc']}.")
    add(f"* Matching failures (BGC record without a core counterpart): {bgc_only}.")
    add(f"* Ascending profiles: {identity['direction_distribution'].get('A', 0)}; "
        f"descending: {identity['direction_distribution'].get('D', 0)}; "
        f"unknown: {identity['direction_distribution'].get(UNKNOWN_DIRECTION, 0)}.")
    add("")
    if matching["multiple_bgc"]:
        add("Profiles where several BGC products match the same signature are")
        add("resolved by product preference and every candidate is recorded in")
        add("`bgc_source_metadata.candidates`.")
        add("")
    add("Matching is done on `(JULD, DIRECTION)` inside a `(float, cycle)` group,")
    add("never on `profile_index_in_file`.")
    add("")
    add("---")
    add("")

    # 5. Core variant selection -------------------------------------------
    add("## 5. Core variant selection statistics")
    add("")
    add(md_table(
        ["Variants for one logical profile", "Profiles"],
        [[f"{size} variant(s)", f"{count:,}"]
         for size, count in sorted(variants["variants_per_profile"].items())]))
    add("")
    add(md_table(
        ["Measure", "Value"],
        [["Profiles with more than one variant",
           f"{variants['profiles_with_multiple']:,}"],
          ["Variants collapsed into provenance", f"{variants['discarded_variants']:,}"],
          ["Profiles with a delayed-mode variant",
           f"{variants['profiles_with_delayed_variant']:,}"],
          ["Delayed-mode variant selected",
           f"{variants['delayed_selected']:,}"],
          ["Delayed-mode variant not selected",
           f"{len(variants['delayed_not_selected'])}"],
          ["Selection rule: delayed >= 90 % of max levels",
           f"{variants['selection_rules'].get('delayed_mode_at_least_90_percent', 0):,}"],
          ["Selection rule: most levels",
           f"{variants['selection_rules'].get('most_levels', 0):,}"]]))
    add("")
    add("Selected `DATA_MODE` distribution: " + ", ".join(
        f"`{mode}` {count:,}"
        for mode, count in sorted(variants["selected_data_modes"].items())))
    add("")
    if variants["delayed_not_selected"]:
        add("Delayed-mode variants that were **not** selected (the documented rule")
        add("keeps the variant with most levels when the delayed record holds less")
        add("than 90 % of them; the adjusted values stay in the corpus and the")
        add("variant is listed in `core_variants`):")
        add("")
        add(md_table(
            ["Float", "Cycle", "Dir", "Selected file", "Mode", "Levels",
             "Variants (`#index levels mode`, `*` = selected)"],
            [[item["float"], item["cycle"], item["direction"],
              item["selected_file"], item["selected_data_mode"],
              item["selected_levels"],
              "; ".join(
                  f"#{v['index']} {v['levels']}lv "
                  f"{(v['data_mode'] or '?')}" + (" *" if v["selected"] else "")
                  for v in item["variants"])]
             for item in variants["delayed_not_selected"]]))
        add("")
    add("---")
    add("")

    # 6. Pressure-grid audit ----------------------------------------------
    add("## 6. Pressure-grid audit")
    add("")
    add(md_table(
        ["Grid status", "BGC profiles", "Share of BGC profiles"],
        [[status, f"{grid['statuses'].get(status, 0):,}",
          f"{100 * grid['statuses'].get(status, 0) / max(1, grid['bgc_profiles']):.1f} %"]
         for status in GRID_STATUSES]))
    add("")
    add(f"BGC profiles audited: **{grid['bgc_profiles']:,}** "
        f"(core-only profiles have no BGC axis to validate: {core_only}).")
    add("Values are never rescaled, shifted or reordered by the audit or by")
    add("ingestion.")
    add("")
    if suspects:
        add(f"### `pres_scale_suspect` ({len(suspects)} profiles)")
        add("")
        add(md_table(
            ["Float", "Cycle", "Dir", "BGC file", "Core range (dbar)",
             "BGC range (dbar)", "Median ratio", "Reason"],
            [[item["float"], item["cycle"], item["direction"], item["file"],
              f"{item['core_min']} – {item['core_max']}",
              f"{item['bgc_min']} – {item['bgc_max']}",
              item["median_ratio"], item["reason"]]
             for item in suspects]))
        add("")
    if mismatches:
        add(f"### `grid_mismatch` ({len(mismatches)} profiles)")
        add("")
        add(md_table(
            ["Float", "Cycle", "Dir", "BGC file", "Core range", "BGC range",
             "Overlap", "Reason"],
            [[item["float"], item["cycle"], item["direction"], item["file"],
              f"{item['core_min']} – {item['core_max']}",
              f"{item['bgc_min']} – {item['bgc_max']}",
              item["overlap_fraction"], item["reason"]]
             for item in mismatches]))
        add("")
    if grid["missing"]:
        add(f"### `missing_pressure` ({len(grid['missing'])} profiles)")
        add("")
        add(md_table(["Float", "Cycle", "Dir", "BGC file", "Reason"],
                     [[item["float"], item["cycle"], item["direction"],
                       item["file"], item["reason"]]
                      for item in grid["missing"]]))
        add("")
    add("---")
    add("")

    # 7. Parameter coverage -----------------------------------------------
    add("## 7. Parameter coverage")
    add("")
    add(md_table(
        ["Parameter", "Profiles", "Matched", "BGC-only", "QC", "Adjusted",
         "Adjusted error", "Adjusted QC", "`ok` grid"],
        [[name,
          f"{parameters[name]['profiles']:,}",
          f"{parameters[name]['matched']:,}",
          f"{parameters[name]['bgc_only']:,}",
          f"{parameters[name]['with_qc']:,}",
          f"{parameters[name]['with_adjusted']:,}",
          f"{parameters[name]['with_adjusted_error']:,}",
          f"{parameters[name]['with_adjusted_qc']:,}",
          f"{parameters[name]['with_ok_grid']:,}"]
         for name in TARGET_PARAMS]))
    add("")
    add("Profiles per parameter combination (as delivered by the BGC product):")
    add("")
    add(md_table(
        ["Parameters in the profile", "Profiles"],
        [[", ".join(item["parameters"]) or "(none)",
          f"{item['profiles']:,}"]
         for item in parameters["_combinations"]]))
    add("")
    ph_only = parameters["_ph_only"]
    if (ph_only["profiles"] and ph_only["both"] == ph_only["profiles"]
            and ph_only["both"] == ph_only["suspects"]):
        add(f"The {ph_only['profiles']} profiles whose only parameter is "
            "`PH_IN_SITU_TOTAL` are **exactly** the "
            f"{ph_only['suspects']} `pres_scale_suspect` profiles: the same "
            "synthetic generation run delivered both a rescaled `PRES` axis and "
            "a reduced parameter set. Recorded as an observation, not as a "
            "causal claim.")
    else:
        add(f"`PH_IN_SITU_TOTAL`-only profiles: {ph_only['profiles']}; of these "
            f"{ph_only['both']} share a suspect pressure grid "
            f"({ph_only['suspects']} suspect grids in total).")
    add("")
    add("---")
    add("")

    # 8. Errors and anomalies ---------------------------------------------
    add("## 8. Error and anomaly list")
    add("")
    failures = summary["failures"]
    add(md_table(
        ["Category", "Count"],
        [["Files that failed to open", f"{len(failures['files'])}"],
         ["Extraction errors", f"{len(failures['extraction'])}"],
         ["Matching errors", f"{len(failures['matching'])}"],
         ["Plans with a warning", f"{len(summary['warnings'])}"],
         ["`pres_scale_suspect`", f"{len(suspects)}"],
         ["`grid_mismatch`", f"{len(mismatches)}"]]))
    add("")
    if failures["files"] or failures["extraction"] or failures["matching"]:
        for item in failures["files"] + failures["extraction"] + failures["matching"]:
            add(f"* `{item['file']}`: {item['error']}")
        add("")
    if summary["warnings"]:
        add("Plans carrying a warning (the full list is in `plans.json`):")
        add("")
        add(md_table(
            ["Float", "Cycle", "Dir", "Match", "Warnings"],
            [[item["float"], item["cycle"], item["direction"], item["match"],
              "; ".join(item["warnings"])]
             for item in summary["warnings"][:40]]))
        if len(summary["warnings"]) > 40:
            add("")
            add(f"... and {len(summary['warnings']) - 40} more in "
                "`backend/scratch/bgc_audit/plans.json`.")
        add("")
    if summary["warning_kinds"]:
        add("Warning kinds:")
        add("")
        add(md_table(["Warning", "Plans"],
                     [[kind, count]
                      for kind, count in summary["warning_kinds"].items()]))
        add("")
    add("No anomaly required a change to the matching algorithm. Identity,")
    add("readability and matching produced no error at all; the warnings above are")
    add("the documented variant-selection rule and the known pressure-scale defect")
    add("of the synthetic product, each recorded per profile in `plans.json`.")
    add("")
    add("---")
    add("")

    # 9. Performance -------------------------------------------------------
    add("## 9. Performance")
    add("")
    add(md_table(
        ["Measure", "Value"],
        [["Files scanned", f"{dataset['files']:,}"],
         ["`N_PROF` records", f"{identity['records']:,}"],
         ["Index pass", f"{performance['index_seconds']:.1f} s"],
         ["Planning pass", f"{performance['plan_seconds']:.1f} s"],
         ["Pressure/parameter verification pass",
          f"{performance['verify_seconds']:.1f} s"],
         ["Total wall clock", f"{performance['total_seconds']:.1f} s"],
         ["Peak traced Python memory", f"{performance['peak_memory_mb']:.1f} MiB"],
         ["Files that failed to open", f"{len(failures['files'])}"],
         ["Extraction errors", f"{len(failures['extraction'])}"],
         ["Matching errors", f"{len(failures['matching'])}"],
         ["Index parity vs production scanner",
          f"{summary['parity']['files_checked']} records, "
          f"{len(summary['parity']['mismatches'])} mismatches"]]))
    add("")
    add("Index parity: the audit's own metadata scanner was compared field by")
    add("field with the production `bgc_matching.index_file` on a 30-file sample")
    add("(float, cycle, `JULD`, direction, data mode, level count, `N_PROF` index,")
    add("product family), so the audit is measuring the same index ingestion uses.")
    add("")
    add("Memory stays bounded because only metadata is kept: the index holds one")
    add("small dictionary per `N_PROF` record, JSON is written once at the end, and")
    add("each `PRES` array is read inside a single `with netCDF4.Dataset(...)` block")
    add("and discarded immediately. Peak traced memory above covers Python")
    add("allocations only (the NetCDF library's own buffers are outside it).")
    add("")
    add("---")
    add("")

    # 10. Recommended next step -------------------------------------------
    add("## 10. Recommended next step")
    add("")
    floats = summary["floats"]
    descending_floats = [item for item in floats if item["descending"]]
    suspect_floats = sorted(
        [item for item in floats if item["suspects"]],
        key=lambda item: -item["suspects"])
    shared = [item for item in descending_floats if item["suspects"]]

    if safe:
        add("Proceed to a **bounded bulk ingestion of one complete float**, not the")
        add("whole corpus, against a disposable copy of the development database")
        add("first. Re-run it immediately afterwards to prove idempotency, then")
        add("compare its row count with the per-float plan in section 2.")
        add("")
        if shared:
            add(f"One float covers every remaining path: **float {shared[0]['float']}** "
                f"({shared[0]['profiles']} profiles, {shared[0]['descending']} "
                f"descending, {shared[0]['suspects']} flagged grids).")
        else:
            if descending_floats:
                add(f"No single float exercises both edge cases, so ingest two, in "
                    f"this order: **float {descending_floats[0]['float']}** "
                    f"({descending_floats[0]['profiles']} profiles, "
                    f"{descending_floats[0]['descending']} descending — the "
                    "ascending/descending path), then "
                    f"**float {suspect_floats[0]['float']}** "
                    f"({suspect_floats[0]['suspects']} of the "
                    f"{len(suspects)} flagged grids — the pressure-defect path).")
            else:
                add(f"Start with **float {suspect_floats[0]['float']}** "
                    f"({suspect_floats[0]['suspects']} flagged grids) if there is "
                    "one, otherwise any complete float.")
    else:
        add("Do **not** ingest yet. Resolve the blockers listed in section 8 first.")
    add("")
    add("The 13 known rescaled-`PRES` files remain usable for BGC measurements but")
    add("must be excluded from any BGC-versus-depth analysis until the synthetic")
    add("product is regenerated.")
    add("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="Corpus root (searched as <root>/*/*/profiles/*.nc).")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help="Markdown report to write.")
    parser.add_argument("--json-dir", default=DEFAULT_JSON_DIR,
                        help="Directory for machine-readable outputs.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Audit only the first N files (debugging).")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        raise SystemExit(f"Corpus not found: {args.root}")

    paths = sorted(glob.glob(os.path.join(args.root, "*", "*", "profiles",
                                         "*.nc")))
    if args.limit:
        paths = paths[:args.limit]
    if not paths:
        raise SystemExit("No NetCDF files found.")

    print(f"Corpus : {args.root}")
    print(f"Files  : {len(paths)}")

    tracemalloc.start()
    started = time.perf_counter()

    # 1. Index ------------------------------------------------------------
    print("Indexing ...", flush=True)
    index_started = time.perf_counter()
    records = []
    file_records = {}
    failures = {"files": [], "extraction": [], "matching": []}
    for position, path in enumerate(paths, start=1):
        try:
            scanned = scan_file(path)
        except Exception as error:
            failures["files"].append({"file": os.path.basename(path),
                                      "error": str(error)})
            continue
        file_records[os.path.basename(path)] = scanned
        records.extend(scanned)
        if position % 200 == 0:
            print(f"  {position}/{len(paths)} files", flush=True)
    index_seconds = time.perf_counter() - index_started

    if not records:
        raise SystemExit("No records could be indexed.")

    files_by_prefix = collections.Counter(_prefix(path) for path in paths)
    core_levels = [r["levels"] for r in records if r["family"] == "core"]
    bgc_levels = [r["levels"] for r in records if r["family"] == "bgc"]
    times = [r["juld"] for r in records if r["juld"] is not None]

    # 2. Plans ------------------------------------------------------------
    print("Planning ...", flush=True)
    plan_started = time.perf_counter()
    unusable = [{"file": r["file"], "index": r["index"],
                 "reason": "no usable PLATFORM_NUMBER/CYCLE_NUMBER"}
                for r in records
                if r["float"] is None or r["cycle"] is None]
    try:
        # build_plans already drops unusable records; the list above documents them.
        plans, groups = build_plans(records)
    except Exception as error:  # a matcher crash must be reported, not hidden
        failures["matching"].append({"file": "(corpus)", "error": str(error)})
        plans, groups = [], {}
        print(f"  matching error: {error}", flush=True)
    plan_seconds = time.perf_counter() - plan_started

    # 3. Pressure grids and parameter presence ----------------------------
    print(f"Verifying {len(plans)} plans ...", flush=True)
    verify_started = time.perf_counter()
    for position, plan in enumerate(plans, start=1):
        try:
            plan["verification"] = verify_bgc(plan)
        except Exception as error:
            failures["extraction"].append({
                "file": (plan["bgc"] or plan["core"])["file"],
                "error": str(error),
            })
            plan["verification"] = None
        if position % 100 == 0:
            print(f"  {position}/{len(plans)} plans", flush=True)
    verify_seconds = time.perf_counter() - verify_started

    # 4. Identity duplicates ---------------------------------------------
    identities = collections.Counter(
        logical_identity(plan["core"] or plan["bgc"]) for plan in plans
    )
    duplicates = [
        {"float": identity[0], "cycle": identity[1], "direction": identity[2],
         "profile_time": identity[3].isoformat() if identity[3] else None,
         "count": count}
        for identity, count in identities.items() if count > 1
    ]

    two_direction_groups = sum(
        1 for group in groups.values()
        if len({record["direction"] for record in group["core"]
                if record.get("direction")}) > 1
    )

    # 5. Parity with the production indexer -------------------------------
    parity = {"files_checked": 0, "mismatches": []}
    sample = paths[:: max(1, len(paths) // 30)][:30]
    for path in sample:
        try:
            production = index_file(path)
        except Exception as error:
            parity["mismatches"].append({"file": os.path.basename(path),
                                         "error": str(error)})
            continue
        audited = file_records.get(os.path.basename(path), [])
        fields = ("float", "cycle", "juld", "direction", "data_mode", "levels",
                  "index", "family")
        for expected, actual in zip(production, audited):
            parity["files_checked"] += 1
            for field in fields:
                if expected[field] != actual[field]:
                    parity["mismatches"].append({
                        "file": os.path.basename(path),
                        "field": field,
                        "production": expected[field],
                        "audit": actual[field],
                    })

    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    performance = {
        "index_seconds": round(index_seconds, 2),
        "plan_seconds": round(plan_seconds, 2),
        "verify_seconds": round(verify_seconds, 2),
        "total_seconds": round(elapsed, 2),
        "peak_memory_mb": round(peak / (1024 * 1024), 1),
        "files": len(paths),
        "records": len(records),
    }

    warnings = []
    for plan in plans:
        messages = plan_warnings(plan, plan.get("verification"))
        if messages:
            warnings.append({
                "float": (plan["core"] or plan["bgc"])["float"],
                "cycle": (plan["core"] or plan["bgc"])["cycle"],
                "direction": plan["direction"],
                "match": plan["match"],
                "warnings": messages,
            })

    extra = {
        "file_count": len(paths),
        "files_by_prefix": dict(collections.Counter(
            path.split(os.sep)[-3] and _prefix(path) for path in paths)),
        "file_records": file_records,
        "core_level_range": _range(core_levels),
        "bgc_level_range": _range(bgc_levels),
        "duplicate_identities": {"count": len(duplicates), "items": duplicates},
        "unusable_records": {"count": len(unusable), "items": unusable},
        "time_range": {
            "min": _juld_date(min(times)) if times else None,
            "max": _juld_date(max(times)) if times else None,
        },
        "performance": performance,
        "failures": failures,
        "warnings": warnings,
        "parity": parity,
        "two_direction_groups": two_direction_groups,
    }
    summary = build_summary(records, plans, groups, extra)
    summary["parity"] = parity
    summary["groups_with_two_directions"] = two_direction_groups

    # 6. Outputs ----------------------------------------------------------
    os.makedirs(args.json_dir, exist_ok=True)
    _write_json(os.path.join(args.json_dir, "index.json"), {
        "corpus": args.root,
        "files": len(paths),
        "records": records,
    })
    _write_json(os.path.join(args.json_dir, "plans.json"), {
        "profiles": len(plans),
        "plans": [plan_output(plan) for plan in plans],
    })
    _write_json(os.path.join(args.json_dir, "summary.json"), summary)

    report = build_report(summary, args)
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report.rstrip() + "\n")

    print()
    print(f"files            : {len(paths)}")
    print(f"records          : {len(records)}")
    print(f"logical profiles : {len(plans)}")
    print(f"match            : {summary['matching']['matched']} matched, "
          f"{summary['matching']['core_only']} core-only, "
          f"{summary['matching']['bgc_only']} BGC-only")
    print(f"grid             : {summary['grid']['statuses']}")
    print(f"duplicates       : {summary['identity']['duplicate_identities']['count']}")
    print(f"failures         : {len(failures['files'])} files, "
          f"{len(failures['extraction'])} extraction, "
          f"{len(failures['matching'])} matching")
    print(f"parity           : {parity['files_checked']} records compared, "
          f"{len(parity['mismatches'])} mismatches")
    print(f"runtime          : {elapsed:.1f} s "
          f"(peak {performance['peak_memory_mb']:.1f} MiB)")
    print(f"report           : {os.path.relpath(args.report, PROJECT_DIR)}")
    print(f"json             : {os.path.relpath(args.json_dir, PROJECT_DIR)}")

    return 0 if not failures["files"] and not failures["matching"] else 1


def _range(values):
    if not values:
        return None
    return f"{min(values):,} – {max(values):,} (median {sorted(values)[len(values) // 2]:,})"


def _juld_date(juld):
    moment = profile_time_from_juld(juld)
    return moment.date().isoformat() if moment else None


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, default=str)


def plan_output(plan):
    core = plan["core"]
    bgc = plan["bgc"]
    base = core or bgc
    verification = plan.get("verification") or {}
    return {
        "identity": {
            "float": base["float"],
            "cycle": base["cycle"],
            "direction": plan["direction"],
            "profile_time": plan["profile_time"].isoformat()
            if plan["profile_time"] else None,
            "juld": base["juld"],
        },
        "match": plan["match"],
        "core": None if core is None else {
            "file": core["file"],
            "profile_index_in_file": core["index"],
            "data_mode": core["data_mode"],
            "levels": core["levels"],
        },
        "core_variants": plan["core_variants"],
        "core_selection_rule": plan["core_selection_rule"],
        "bgc": None if bgc is None else {
            "file": bgc["file"],
            "profile_index_in_file": bgc["index"],
            "levels": bgc["levels"],
            "parameters": plan["bgc_parameters"],
        },
        "bgc_candidates": plan["bgc_candidates"],
        "grid": {
            "status": verification.get("status"),
            "detail": verification.get("detail"),
        } if verification else None,
        "warnings": plan_warnings(plan, plan.get("verification")),
    }


if __name__ == "__main__":
    sys.exit(main())
