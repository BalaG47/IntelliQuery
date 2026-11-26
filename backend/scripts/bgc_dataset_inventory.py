"""Metadata-only inventory of the local BGC-Argo NetCDF corpus.

Reads only file headers and profile-level metadata (no measurement arrays are
materialised beyond what is needed to confirm availability), so it can be run
against the full 1,264-file dataset in a few seconds.

Usage (from ``backend/``):

    python scripts/bgc_dataset_inventory.py
    python scripts/bgc_dataset_inventory.py --json inventory.json

The script never writes to the dataset directory.
"""

import argparse
import collections
import glob
import json
import os
import sys

import netCDF4
import numpy as np

DEFAULT_ROOT = os.path.join("bgc data", "bgc raw data")

TARGET_BGC = ["DOXY", "CHLA", "NITRATE", "BBP700", "PH_IN_SITU_TOTAL"]
CORE = ["PRES", "TEMP", "PSAL"]


def profile_metadata(path):
    """Return one metadata record per profile in ``path``.

    Uses ``netCDF4`` directly rather than ``xarray``: the inventory only needs
    scalar/profile-level variables, and opening 1,264 files through xarray's
    full attribute/lazy-load machinery is far slower.
    """
    with netCDF4.Dataset(path, "r") as ds:
        sizes = {name: len(dim) for name, dim in ds.dimensions.items()}
        n_prof = sizes.get("N_PROF", 1)

        def char_rows(name):
            """Decode a 2-D (N_PROF, N_PARAM) character variable into strings.

            netCDF4 exposes Argo character variables either as fixed-width
            byte strings (S16) or, in classic files, as (N_PROF, N_PARAM, len)
            arrays of single characters. Both shapes are normalised here.
            """
            if name not in ds.variables:
                return []
            data = ds.variables[name][:]
            data = np.ma.filled(data, b" ")
            if data.ndim == 3:
                data = np.array([
                    [b"".join(c for c in item) for item in row]
                    for row in data
                ])
            elif data.ndim == 1:
                data = np.array([data])
            out = []
            for row in data:
                out.append([
                    (c.decode("utf-8", errors="replace").strip()
                     if isinstance(c, (bytes, np.bytes_)) else str(c).strip())
                    for c in np.atleast_1d(row)
                ])
            return out

        station = char_rows("STATION_PARAMETERS")
        pdm = char_rows("PARAMETER_DATA_MODE")

        def prof_values(name):
            if name not in ds.variables:
                return [None] * n_prof
            data = ds.variables[name][:]
            if data.ndim == 0:
                return [data[()]] * n_prof
            return [v for v in np.atleast_1d(data)]

        cycles = prof_values("CYCLE_NUMBER")
        julds = prof_values("JULD")
        lats = prof_values("LATITUDE")
        lons = prof_values("LONGITUDE")
        modes = prof_values("DATA_MODE")

        format_version = None
        if "FORMAT_VERSION" in ds.variables:
            format_version = _text(np.atleast_1d(ds.variables["FORMAT_VERSION"][:])[0])

        variables = set(ds.variables)

    records = []
    for i in range(n_prof):
        names = station[i] if i < len(station) else []
        names = [n for n in names if n]
        records.append({
            "n_prof": int(n_prof),
            "profile_index": i,
            "cycle": _num(cycles[i]) if i < len(cycles) else None,
            "juld": _num(julds[i]) if i < len(julds) else None,
            "lat": _num(lats[i]) if i < len(lats) else None,
            "lon": _num(lons[i]) if i < len(lons) else None,
            "data_mode": _text(modes[i]) if i < len(modes) else None,
            "station_parameters": [n for n in names if n],
            "parameter_data_mode": pdm[i] if i < len(pdm) else [],
            "format_version": format_version,
            "n_levels": sizes.get("N_LEVELS"),
            "n_param": sizes.get("N_PARAM"),
            "variables": sorted(variables),
        })
    return records


def _text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip("\x00 \t\r\n")
    return value or None


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # drop NaN


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"dataset root not found: {args.root}", file=sys.stderr)
        return 2

    paths = sorted(glob.glob(os.path.join(args.root, "*", "*", "profiles", "*.nc")))
    if not paths:
        print(f"no NetCDF files under {args.root}", file=sys.stderr)
        return 2

    unreadable = []
    entries = []
    for path in paths:
        rel = os.path.relpath(path, args.root).replace("\\", "/")
        parts = rel.split("/")
        try:
            records = profile_metadata(path)
        except Exception as error:  # unreadable / corrupt file
            unreadable.append({"file": rel, "error": f"{type(error).__name__}: {error}"})
            continue
        for record in records:
            record["file"] = rel
            record["dac"] = parts[0]
            record["float"] = parts[1]
            record["filename"] = parts[-1]
            record["prefix"] = parts[-1][:2]
            entries.append(record)

    summary = summarise(entries, unreadable)
    print(json.dumps(summary, indent=2, default=str))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "profiles": entries}, handle,
                      indent=2, default=str)
        print(f"\nfull inventory written to {args.json}", file=sys.stderr)
    return 0


def summarise(entries, unreadable):
    files = {e["file"] for e in entries}
    prefix = collections.Counter(e["prefix"] for e in entries)
    dac = collections.Counter(e["dac"] for e in entries)

    floats = collections.defaultdict(set)
    for e in entries:
        floats[e["float"]].add(e["file"])

    param_counts = collections.Counter()
    for e in entries:
        for name in set(e["station_parameters"]):
            param_counts[name] += 1
    var_counts = collections.Counter()
    for e in entries:
        for name in e["variables"]:
            if name in TARGET_BGC:
                var_counts[name] += 1

    modes = collections.Counter()
    for e in entries:
        for mode in e["parameter_data_mode"]:
            if mode:
                modes[mode] += 1

    multi = [e for e in entries if e["n_prof"] > 1]
    nan_position = [e for e in entries if e["lat"] is None or e["lon"] is None]
    years = collections.Counter()
    for e in entries:
        if e["juld"] is not None:
            # Argo time origin, matching pandas behaviour in data_processor
            from datetime import datetime, timedelta
            years[(datetime(1950, 1, 1) + timedelta(days=e["juld"])).year] += 1

    # (float, cycle) collisions, both inside a file and across the corpus
    per_float_cycle = collections.Counter((e["float"], e["cycle"]) for e in entries)
    colliding = {f"{f}/{c}": n for (f, c), n in per_float_cycle.items() if n > 1}

    levels = sorted({e["n_levels"] for e in entries if e["n_levels"]})

    return {
        "files": len(files),
        "profiles": len(entries),
        "unreadable": unreadable,
        "dac_distribution": dict(dac),
        "prefix_distribution_files": dict(collections.Counter(
            e["prefix"] for e in entries if e["profile_index"] == 0)),
        "prefix_distribution_profiles": dict(prefix),
        "floats": {k: len(v) for k, v in sorted(floats.items())},
        "unique_floats": len(floats),
        "parameter_available_profiles": dict(param_counts.most_common()),
        "target_bgc_variable_present": dict(var_counts),
        "parameter_data_modes": dict(modes),
        "multi_profile_profiles": len(multi),
        "multi_profile_files": len({e["file"] for e in multi}),
        "profiles_without_position": len(nan_position),
        "float_cycle_collisions": {
            "count": len(colliding),
            "examples": dict(list(colliding.items())[:10]),
        },
        "years": dict(sorted(years.items())),
        "n_levels_values": levels[:20],
        "n_levels_max": max(levels) if levels else None,
        "format_versions": dict(collections.Counter(
            str(e["format_version"]) for e in entries)),
    }


if __name__ == "__main__":
    raise SystemExit(main())
