"""Associate core (D/R) and biogeochemical (SD/SR/BD/BR) profiles of one cycle.

The investigation in ``docs/bgc/BGC_PRODUCT_MATCHING.md`` established that the
core and synthetic-BGC products of this corpus describe the *same* physical
profiles, that every core file's ``N_PROF`` entries are processing variants of
one profile, and that neither product carries a profile identifier. This module
implements the resulting rules:

* the logical profile identity is
  ``(float_id, cycle_number, direction, profile_time)``;
* profiles are matched on ``(JULD, DIRECTION)`` inside a ``(float, cycle)``
  group — never on ``profile_index_in_file``, which is provenance only;
* one representative core variant is chosen (delayed mode when it covers at
  least 90 % of the maximum level count, else the variant with most levels);
* the BGC product's pressure axis is validated, not rescaled.

Everything here is read-only and metadata-first: files are opened with
``netCDF4`` (xarray costs seconds per file in this environment) and measurement
arrays are only counted, never returned. The NetCDF files are never written to.
"""

import collections
import os
from datetime import datetime, timedelta

import netCDF4
import numpy as np

# ---------------------------------------------------------------------------
# Product classification
# ---------------------------------------------------------------------------

CORE_PREFIXES = ("D", "R")
BGC_PREFIXES = ("SD", "SR", "BD", "BR")

# A real BGC product (BD/BR) is preferred over a synthetic one (SD/SR) when both
# match the same signature; the others are still recorded in the provenance.
BGC_TYPE_PRIORITY = {"BD": 0, "BR": 1, "SD": 2, "SR": 3}
DATA_MODE_PRIORITY = {"D": 0, "R": 1, "A": 2}

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

#: A delayed-mode core variant is representative only if it keeps this much of
#: the maximum level count found among the variants of the same profile.
DELAYED_LEVEL_FRACTION = 0.9

#: Direction value stored for rows whose direction cannot be established.
UNKNOWN_DIRECTION = "-"

#: Pressure grid validation statuses stored in ``bgc_source_metadata``.
GRID_OK = "ok"
GRID_PRES_SCALE_SUSPECT = "pres_scale_suspect"
GRID_MISSING_PRESSURE = "missing_pressure"
GRID_MISMATCH = "grid_mismatch"

#: Plausible pressure range in dbar. A near-surface sensor may read slightly
#: negative (e.g. -0.29 dbar), so the lower bound allows that instead of
#: treating a real surface level as an anomaly.
MIN_PLAUSIBLE_PRESSURE = -5.0
MAX_PLAUSIBLE_PRESSURE = 12000.0

#: A BGC/core median-pressure ratio in this range means the BGC axis is
#: approximately 1000x off, which is the known synthetic-product defect.
SCALE_SUSPECT_RANGE = (100.0, 10000.0)

#: How much of the core pressure range the BGC grid must cover to be usable.
MIN_CORE_RANGE_FRACTION = 0.9


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


def file_prefix(path):
    """Leading letters of the file name: ``D``, ``R``, ``SD``, ``SR``, ..."""
    prefix = ""
    for char in os.path.basename(path):
        if not char.isalpha():
            break
        prefix += char
    return prefix.upper()


def product_family(path):
    """``'bgc'`` for SD/SR/BD/BR, ``'core'`` otherwise.

    An unrecognised prefix is treated as core so that a single uploaded file
    keeps the behaviour it had before grouping existed; its provenance still
    records ``product_type='unknown'``.
    """
    if file_prefix(path) in BGC_PREFIXES:
        return "bgc"
    return "core"


def first_float_id(dataset):
    """WMO number from ``PLATFORM_NUMBER`` (N_PROF, STRING8) as a string."""
    if "PLATFORM_NUMBER" not in dataset.variables:
        return None
    raw = np.ravel(np.ma.filled(dataset.variables["PLATFORM_NUMBER"][:], b" "))
    parts = []
    for item in raw[:100]:
        if isinstance(item, (bytes, np.bytes_)):
            parts.append(bytes(item))
        else:
            parts.append(str(item).encode("utf-8", "replace"))
    digits = "".join(
        char for char in b"".join(parts).decode("utf-8", "replace")
        if char.isdigit()
    )
    return digits[:7] or None


def profile_time_from_juld(juld_days):
    """``JULD`` (days since 1950-01-01) as a whole-second ``datetime``.

    The column is a MySQL ``DATETIME`` (no fractional seconds), and MySQL
    *rounds* a fractional value rather than truncating it. Rounding here, once,
    half-up — the same rule MySQL applies — keeps the Python-side identity
    identical to the stored one, which is what makes re-ingestion idempotent.
    """
    if juld_days is None:
        return None
    seconds = float(juld_days) * 86400.0
    return datetime(1950, 1, 1) + timedelta(
        seconds=float(np.floor(seconds + 0.5)))


def logical_identity(record):
    """The stored logical identity of an indexed profile record."""
    return (
        int(record["float"]),
        int(record["cycle"]),
        record.get("direction") or UNKNOWN_DIRECTION,
        profile_time_from_juld(record["juld"]),
    )


# ---------------------------------------------------------------------------
# Metadata index
# ---------------------------------------------------------------------------


def index_file(path):
    """One metadata record per ``N_PROF`` entry of a NetCDF file.

    Only profile-level metadata and the number of valid ``PRES`` levels are
    read; measurement arrays are counted, never kept.
    """
    family = product_family(path)
    records = []
    with netCDF4.Dataset(path, "r") as dataset:
        sizes = {name: len(dim) for name, dim in dataset.dimensions.items()}
        n_prof = sizes.get("N_PROF", 1)
        float_id = first_float_id(dataset)
        direction_available = "DIRECTION" in dataset.variables

        def profile_values(name):
            if name not in dataset.variables:
                return [None] * n_prof
            data = dataset.variables[name][:]
            if data.ndim == 0:
                return [data[()]] * n_prof
            return list(np.atleast_1d(data))[:n_prof]

        cycles = profile_values("CYCLE_NUMBER")
        julds = profile_values("JULD")
        directions = profile_values("DIRECTION")
        modes = profile_values("DATA_MODE")
        lats = profile_values("LATITUDE")
        lons = profile_values("LONGITUDE")

        levels = []
        for index in range(n_prof):
            if "PRES" not in dataset.variables:
                levels.append(0)
                continue
            masked = np.ma.getmaskarray(dataset.variables["PRES"][index])
            levels.append(int(np.size(masked) - np.sum(masked)))

    for index in range(n_prof):
        cycle = _number(cycles[index])
        records.append({
            "path": os.fspath(path),
            "file": os.path.basename(path),
            "prefix": file_prefix(path),
            "family": family,
            "index": index,
            "n_prof": n_prof,
            "float": float_id,
            "cycle": None if cycle is None else int(cycle),
            "juld": _number(julds[index]),
            "direction": _text(directions[index]),
            "direction_available": direction_available,
            "data_mode": _text(modes[index]),
            "latitude": _number(lats[index]),
            "longitude": _number(lons[index]),
            "levels": levels[index],
        })
    return records


def group_by_cycle(records):
    """``(float, cycle)`` -> ``{'core': [...], 'bgc': [...]}``, in file order."""
    groups = collections.OrderedDict()
    for record in records:
        key = (record["float"], record["cycle"])
        bucket = groups.setdefault(key, {"core": [], "bgc": []})
        bucket["bgc" if record["family"] == "bgc" else "core"].append(record)
    return groups


def signature(record):
    """Physical-profile signature: observation time and direction.

    A record without ``JULD`` cannot be grouped with anything (two unknown
    times are not evidence of the same profile), so it gets its own signature.
    """
    juld = record.get("juld")
    if juld is None:
        return ("no-juld", record["path"], record["index"])
    return (round(float(juld), 6), record.get("direction"))


# ---------------------------------------------------------------------------
# Representative selection
# ---------------------------------------------------------------------------


def select_representative_core(records):
    """Pick one core processing variant of one logical profile.

    Rule (documented in the investigation report):

    1. prefer a delayed-mode variant that keeps at least
       ``DELAYED_LEVEL_FRACTION`` of the maximum level count;
    2. otherwise the variant with the most levels;
    3. ties break on data mode (D > R > A), then file name and N_PROF index.

    Returns ``(chosen_record, variant_summaries, rule)``. ``chosen_record`` is
    ``None`` for an empty input.
    """
    if not records:
        return None, [], None

    ordered = sorted(records, key=lambda r: (r["file"], r["index"]))
    max_levels = max(r["levels"] for r in ordered)

    delayed = [
        r for r in ordered
        if (r.get("data_mode") or "").upper() == "D"
        and max_levels > 0
        and r["levels"] >= DELAYED_LEVEL_FRACTION * max_levels
    ]
    if delayed:
        pool, rule = delayed, "delayed_mode_at_least_90_percent"
    else:
        pool, rule = ordered, "most_levels"

    chosen = sorted(
        pool,
        key=lambda r: (
            -r["levels"],
            DATA_MODE_PRIORITY.get((r.get("data_mode") or "").upper(), 9),
            r["file"],
            r["index"],
        ),
    )[0]

    summaries = [
        {
            "file": r["file"],
            "profile_index_in_file": r["index"],
            "data_mode": r.get("data_mode"),
            "levels": r["levels"],
            "selected": r is chosen,
        }
        for r in ordered
    ]
    return chosen, summaries, rule


def select_bgc_record(records):
    """Pick the BGC record to attach; the others stay in the provenance."""
    if not records:
        return None, []
    ordered = sorted(
        records,
        key=lambda r: (
            BGC_TYPE_PRIORITY.get(r["prefix"], 9),
            r["file"],
            r["index"],
        ),
    )
    candidates = [
        {
            "file": r["file"],
            "profile_index_in_file": r["index"],
            "product_type_prefix": r["prefix"],
            "levels": r["levels"],
            "selected": r is ordered[0],
        }
        for r in ordered
    ]
    return ordered[0], candidates


# ---------------------------------------------------------------------------
# Group plan
# ---------------------------------------------------------------------------

MATCH_MATCHED = "matched"
MATCH_CORE_ONLY = "core_only"
MATCH_BGC_ONLY = "bgc_only"


def plan_group(core_records, bgc_records):
    """Decide what to store for one ``(float, cycle)`` group.

    Every input record ends up in exactly one plan, so no profile is discarded:
    core variants collapse into one plan, a BGC record joins the plan with the
    same signature, and a BGC record without a core counterpart becomes its own
    ``bgc_only`` plan (the fallback the brief requires) instead of being
    matched by guesswork.
    """
    buckets = collections.OrderedDict()

    def bucket(bucket_signature):
        return buckets.setdefault(bucket_signature, {"core": [], "bgc": []})

    for record in core_records:
        bucket(signature(record))["core"].append(record)
    for record in bgc_records:
        bucket(signature(record))["bgc"].append(record)

    plans = []
    for bucket_signature, contents in buckets.items():
        core_chosen, core_variants, rule = select_representative_core(
            contents["core"])
        bgc_chosen, bgc_candidates = select_bgc_record(contents["bgc"])

        base = core_chosen or bgc_chosen
        if core_chosen and bgc_chosen:
            match = MATCH_MATCHED
        elif core_chosen:
            match = MATCH_CORE_ONLY
        else:
            match = MATCH_BGC_ONLY

        plans.append({
            "signature": bucket_signature,
            "juld": base["juld"],
            "direction": (base.get("direction") or UNKNOWN_DIRECTION),
            "profile_time": profile_time_from_juld(base["juld"]),
            "core": core_chosen,
            "bgc": bgc_chosen,
            "core_variants": core_variants,
            "core_selection_rule": rule,
            "bgc_candidates": bgc_candidates,
            "match": match,
        })
    return plans


# ---------------------------------------------------------------------------
# Pressure grid validation
# ---------------------------------------------------------------------------


def _plausible(values):
    return [
        float(value) for value in values
        if value is not None and np.isfinite(float(value))
    ]


def classify_pressure_grid(bgc_pressure, core_pressure=None):
    """Validate the BGC product's own pressure axis.

    Returns ``(status, detail)``. The values are never rescaled or reindexed:
    the BGC arrays keep the axis they were delivered with, and a suspect axis
    is flagged for downstream use. ``core_pressure`` is ``None`` for a profile
    that has no core counterpart, in which case only the intrinsic checks run.
    """
    bgc = _plausible(bgc_pressure or [])
    detail = {
        "bgc_pressure_levels": len(bgc),
        "bgc_pressure_min": None,
        "bgc_pressure_max": None,
        "core_pressure_min": None,
        "core_pressure_max": None,
        "overlap_fraction": None,
        "median_ratio": None,
        "reason": None,
    }
    if not bgc:
        detail["reason"] = "no usable pressure values"
        return GRID_MISSING_PRESSURE, detail

    bgc_min, bgc_max = min(bgc), max(bgc)
    detail["bgc_pressure_min"] = round(bgc_min, 4)
    detail["bgc_pressure_max"] = round(bgc_max, 4)

    core = _plausible(core_pressure or [])
    if core:
        core_min, core_max = min(core), max(core)
        detail["core_pressure_min"] = round(core_min, 4)
        detail["core_pressure_max"] = round(core_max, 4)

    if bgc_min < MIN_PLAUSIBLE_PRESSURE or bgc_max > MAX_PLAUSIBLE_PRESSURE:
        detail["reason"] = "pressure outside the plausible range"
        return GRID_MISMATCH, detail

    if len(bgc) < 2:
        detail["reason"] = "fewer than two pressure levels"
        return GRID_MISMATCH, detail

    if not core:
        return GRID_OK, detail

    if any(later < earlier for earlier, later in zip(bgc, bgc[1:])):
        detail["reason"] = "pressure axis is not monotonic"
        return GRID_MISMATCH, detail

    bgc_median = float(np.median(bgc))
    core_median = float(np.median(core))
    if bgc_median > 0:
        ratio = core_median / bgc_median
        detail["median_ratio"] = round(ratio, 3)
        if SCALE_SUSPECT_RANGE[0] <= ratio <= SCALE_SUSPECT_RANGE[1]:
            detail["reason"] = (
                "BGC pressure is approximately 1000x smaller than the core "
                "pressure of the same profile"
            )
            return GRID_PRES_SCALE_SUSPECT, detail

    core_span = core_max - core_min
    overlap = max(0.0, min(bgc_max, core_max) - max(bgc_min, core_min))
    fraction = 1.0 if core_span <= 0 else overlap / core_span
    detail["overlap_fraction"] = round(fraction, 4)
    if fraction < MIN_CORE_RANGE_FRACTION:
        detail["reason"] = "BGC grid covers too little of the core pressure range"
        return GRID_MISMATCH, detail

    return GRID_OK, detail
