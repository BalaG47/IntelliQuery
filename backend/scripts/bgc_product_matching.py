"""Investigate how core (D/R) and synthetic BGC (SD/SR) profiles correspond.

Read-only, metadata-first: every file is opened once with ``netCDF4`` (xarray
would cost seconds per file), and only profile-level metadata plus the number of
valid PRES levels per profile is read. A handful of representative pairs are then
compared level-by-level to test whether the products repeat the same measurements.

Usage (from ``backend/``):

    python scripts/bgc_product_matching.py
    python scripts/bgc_product_matching.py --json scratch/bgc_matching.json

The dataset is never written to and no database is touched.
"""

import argparse
import collections
import functools
import glob
import json
import os
import sys

import netCDF4
import numpy as np

DEFAULT_ROOT = os.path.join("bgc data", "bgc raw data")
CORE_PREFIXES = ("D", "R")
BGC_PREFIXES = ("SD", "SR", "BD", "BR")
BGC_PARAMS = ("DOXY", "CHLA", "BBP700", "NITRATE", "PH_IN_SITU_TOTAL",
              "CHLA_FLUORESCENCE")


def text(value):
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        flat = list(np.ravel(value))
        if all(isinstance(item, (bytes, np.bytes_)) for item in flat):
            value = b"".join(flat)
        else:
            value = "".join(str(item) for item in flat)
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip("\x00 \t\r\n")
    return value or None


def number(value):
    if value is None or np.ma.is_masked(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def family(prefix):
    if prefix in BGC_PREFIXES:
        return "bgc"
    if prefix in CORE_PREFIXES:
        return "core"
    return "unknown"


def first_float_id(ds):
    """WMO number from PLATFORM_NUMBER (N_PROF, STRING8) character array."""
    if "PLATFORM_NUMBER" not in ds.variables:
        return None
    flat = np.ravel(np.ma.filled(ds.variables["PLATFORM_NUMBER"][:], b" "))
    parts = []
    for item in flat[:100]:
        if isinstance(item, (bytes, np.bytes_)):
            parts.append(bytes(item))
        else:
            parts.append(str(item).encode("utf-8", "replace"))
    digits = "".join(
        char for char in b"".join(parts).decode("utf-8", "replace")
        if char.isdigit())
    return digits[:7] or None


def file_prefix(path):
    """Leading letters of the filename: D, R, SD, SR, BD, BR."""
    prefix = ""
    for char in os.path.basename(path):
        if not char.isalpha():
            break
        prefix += char
    return prefix


def scan_file(path, relpath):
    """One record per N_PROF entry; measurements are only counted, not kept."""
    prefix = file_prefix(path)
    with netCDF4.Dataset(path, "r") as ds:
        sizes = {name: len(dim) for name, dim in ds.dimensions.items()}
        n_prof = sizes.get("N_PROF", 1)

        def profile_values(name):
            if name not in ds.variables:
                return [None] * n_prof
            data = ds.variables[name][:]
            if data.ndim == 0:
                return [data[()]] * n_prof
            return list(np.atleast_1d(data))

        cycles = profile_values("CYCLE_NUMBER")
        julds = profile_values("JULD")
        lats = profile_values("LATITUDE")
        lons = profile_values("LONGITUDE")
        directions = profile_values("DIRECTION")
        modes = profile_values("DATA_MODE")
        float_id = first_float_id(ds)

        station = []
        if "STATION_PARAMETERS" in ds.variables:
            raw = np.ma.filled(ds.variables["STATION_PARAMETERS"][:], b" ")
            if raw.ndim == 3:
                raw = np.array([[b"".join(c for c in row) for row in prof]
                                for prof in raw])
            for row in np.atleast_2d(raw):
                station.append([name for name in (text(item) for item in row)
                                if name])
        while len(station) < n_prof:
            station.append([])

        valid_pres = []
        for index in range(n_prof):
            if "PRES" not in ds.variables:
                valid_pres.append(0)
                continue
            masked = np.ma.getmaskarray(ds.variables["PRES"][index])
            valid_pres.append(int(np.size(masked) - np.sum(masked)))

        variables = set(ds.variables)

    records = []
    for index in range(n_prof):
        names = [name.upper() for name in station[index]]
        records.append({
            "file": relpath,
            "filename": os.path.basename(path),
            "prefix": prefix,
            "family": family(prefix),
            "n_prof": n_prof,
            "n_levels_dim": sizes.get("N_LEVELS"),
            "index": index,
            "float": float_id,
            "cycle": number(cycles[index]),
            "juld": number(julds[index]),
            "lat": number(lats[index]),
            "lon": number(lons[index]),
            "direction": text(directions[index]),
            "data_mode": text(modes[index]),
            "station": names,
            "valid_pres_levels": valid_pres[index],
            "bgc_params": [name for name in names if name in BGC_PARAMS],
            "has_temp": "TEMP" in variables,
            "has_psal": "PSAL" in variables,
            "has_pres": "PRES" in variables,
        })
    return records


def load_corpus(root):
    paths = sorted(glob.glob(os.path.join(root, "*", "*", "profiles", "*.nc")))
    records = []
    for path in paths:
        rel = os.path.relpath(path, root).replace("\\", "/")
        records.extend(scan_file(path, rel))
    return records, len(paths)


def signature(record):
    """The physical profile signature: exact observation time and direction."""
    juld = None if record["juld"] is None else round(record["juld"], 6)
    return (juld, record["direction"])


def position(record):
    if record["lat"] is None or record["lon"] is None:
        return None
    return (round(record["lat"], 6), round(record["lon"], 6))


def group_by_cycle(records):
    groups = collections.defaultdict(list)
    for record in records:
        groups[(record["float"], record["cycle"])].append(record)
    return groups


def report_corpus(records, file_count):
    print("1. Corpus")
    print("-" * 40)
    print(f"files scanned            : {file_count}")
    print(f"profile records (N_PROF) : {len(records)}")
    families = collections.Counter(record["family"] for record in records)
    print(f"records by family        : {dict(families)}")
    print(f"records by prefix        : "
          f"{dict(collections.Counter(record['prefix'] for record in records))}")


def report_groups(groups):
    print("\n2. (float, cycle) groups")
    print("-" * 40)
    both, core_only, bgc_only = [], [], []
    for key, members in groups.items():
        has_core = any(m["family"] == "core" for m in members)
        has_bgc = any(m["family"] == "bgc" for m in members)
        if has_core and has_bgc:
            both.append(key)
        elif has_core:
            core_only.append(key)
        else:
            bgc_only.append(key)

    print(f"groups total             : {len(groups)}")
    print(f"  core + BGC             : {len(both)}")
    print(f"  core only              : {len(core_only)}")
    print(f"  BGC only               : {len(bgc_only)}")
    return both


def report_duplication(groups, both):
    print("\n3. Records versus distinct physical profiles")
    print("-" * 40)
    all_signatures = {signature(record) for record in _dedupe_none(groups)}
    core_records = [r for members in groups.values() for r in members
                    if r["family"] == "core"]
    core_signatures = {signature(r) for r in core_records}
    bgc_records = [r for members in groups.values() for r in members
                   if r["family"] == "bgc"]
    bgc_signatures = {signature(r) for r in bgc_records}

    print(f"core records             : {len(core_records)}")
    print(f"  distinct (JULD, dir)   : {len(core_signatures)}")
    print(f"BGC records              : {len(bgc_records)}")
    print(f"  distinct (JULD, dir)   : {len(bgc_signatures)}")
    print(f"all records              : {len(core_records) + len(bgc_records)}")
    print(f"  distinct (JULD, dir)   : {len(all_signatures)}")

    # How many core records share one signature inside a group?
    multiplicity = collections.Counter()
    for key in both:
        signatures = collections.Counter(
            signature(r) for r in groups[key] if r["family"] == "core")
        for count in signatures.values():
            multiplicity[count] += 1
    print(f"core signature multiplicity per matched group: "
          f"{dict(sorted(multiplicity.items()))}")
    return core_records, bgc_records, core_signatures, bgc_signatures


def _dedupe_none(groups):
    for members in groups.values():
        for member in members:
            yield member


def report_signature_matching(groups, both):
    print("\n4. Do core and BGC products describe the same physical profiles?")
    print("-" * 40)
    exact = subset = superset = overlap = disjoint = 0
    juld_only_mismatch = direction_only = 0
    examples = collections.defaultdict(list)

    for key in both:
        core = {signature(r) for r in groups[key] if r["family"] == "core"}
        bgc = {signature(r) for r in groups[key] if r["family"] == "bgc"}
        if core == bgc:
            exact += 1
            examples["exact"].append(key)
        elif bgc < core:
            subset += 1
            examples["bgc_subset"].append(key)
        elif core < bgc:
            superset += 1
            examples["core_subset"].append(key)
        elif core & bgc:
            overlap += 1
            examples["partial"].append(key)
        else:
            disjoint += 1
            examples["disjoint"].append(key)
            core_juld = {s[0] for s in core}
            bgc_juld = {s[0] for s in bgc}
            if core_juld & bgc_juld:
                direction_only += 1
                examples["direction_mismatch"].append(key)
            elif len(core_juld) == len(bgc_juld) and core_juld != bgc_juld:
                juld_only_mismatch += 1
                examples["time_mismatch"].append(key)

    print(f"groups with both products          : {len(both)}")
    print(f"  identical signature sets         : {exact}")
    print(f"  BGC signatures are a subset      : {subset}")
    print(f"  core signatures are a subset     : {superset}")
    print(f"  partial overlap                  : {overlap}")
    print(f"  no shared signature              : {disjoint}")
    print(f"    same JULD, different DIRECTION : {direction_only}")
    print(f"    different JULD                 : {juld_only_mismatch}")

    for name, keys in examples.items():
        sample = ", ".join(f"{f}/{int(c)}" for f, c in keys[:6])
        print(f"  e.g. {name:<20}: {sample}")
    return examples


def report_candidate_ambiguity(groups, both):
    print("\n5. Candidate core profiles per BGC profile")
    print("-" * 40)
    buckets = collections.Counter()
    strict_buckets = collections.Counter()
    no_candidate = []

    for key in both:
        core = [r for r in groups[key] if r["family"] == "core"]
        bgc = [r for r in groups[key] if r["family"] == "bgc"]
        for record in bgc:
            candidates = [c for c in core if signature(c) == signature(record)]
            strict_buckets[len(candidates)] += 1
            loose = [c for c in core if c["juld"] is not None
                     and record["juld"] is not None
                     and round(c["juld"], 6) == round(record["juld"], 6)]
            buckets[len(loose)] += 1
            if not candidates:
                no_candidate.append((key, record))

    print("matching on (JULD, DIRECTION) -> core candidates per BGC profile")
    for count in sorted(strict_buckets):
        label = {0: "no candidate", 1: "exactly one",
                 }.get(count, "ambiguous (%d candidates)" % count)
        print(f"  {count:>2} candidate(s): {strict_buckets[count]:>4}  {label}")
    print("matching on JULD only -> core candidates per BGC profile")
    for count in sorted(buckets):
        print(f"  {count:>2} candidate(s): {buckets[count]:>4}")
    return no_candidate


def representative_pairs(groups, both, limit=8):
    """Pick pairs covering the structural cases in the brief."""
    chosen = []
    seen_shapes = set()
    extras = [("7902170", 1.0), ("3902581", 6.0)]

    def shape(key):
        members = groups[key]
        core = [r for r in members if r["family"] == "core"]
        bgc = [r for r in members if r["family"] == "bgc"]
        return (len(core), len({signature(r) for r in core}),
                len(bgc), len({signature(r) for r in bgc}))

    for key in sorted(both):
        current = shape(key)
        if current in seen_shapes:
            continue
        seen_shapes.add(current)
        chosen.append(key)
        if len(chosen) >= limit:
            break
    for extra in extras:
        if extra in groups and extra not in chosen:
            chosen.append(extra)
    return chosen


def deep_compare(root, bgc_record, core_record):
    """Compare PRES/TEMP of one BGC profile against one core profile."""
    def levels(record, var):
        path = os.path.join(root, record["file"])
        with netCDF4.Dataset(path, "r") as ds:
            if var not in ds.variables:
                return None
            values = np.ma.filled(
                ds.variables[var][record["index"]].astype("f8"), np.nan)
        return np.atleast_1d(values)

    bgc_pres = levels(bgc_record, "PRES")
    core_pres = levels(core_record, "PRES")
    if bgc_pres is None or core_pres is None:
        return None
    bgc_valid = bgc_pres[~np.isnan(bgc_pres)]
    core_valid = core_pres[~np.isnan(core_pres)]
    if bgc_valid.size == 0 or core_valid.size == 0:
        return None

    bgc_keys = {round(float(value), 3): position for position, value
                in enumerate(bgc_pres)}
    matched = 0
    for value in core_valid:
        if round(float(value), 3) in bgc_keys:
            matched += 1

    # Compare TEMP at exactly matching pressures.
    bgc_temp = levels(bgc_record, "TEMP")
    core_temp = levels(core_record, "TEMP")
    temp_equal = temp_compared = 0
    if bgc_temp is not None and core_temp is not None:
        lookup = {}
        for index, value in enumerate(bgc_pres):
            if not np.isnan(value) and index < bgc_temp.size:
                lookup.setdefault(round(float(value), 3), bgc_temp[index])
        for index, value in enumerate(core_pres):
            if np.isnan(value) or index >= core_temp.size:
                continue
            key = round(float(value), 3)
            if key not in lookup:
                continue
            other = lookup[key]
            if np.isnan(other) or np.isnan(core_temp[index]):
                continue
            temp_compared += 1
            if abs(float(other) - float(core_temp[index])) < 1e-4:
                temp_equal += 1

    return {
        "bgc_levels": int(bgc_valid.size),
        "core_levels": int(core_valid.size),
        "core_levels_found_in_bgc": matched,
        "temp_compared": temp_compared,
        "temp_equal": temp_equal,
    }


def report_pairs(root, groups, pairs):
    print("\n6. Representative (float, cycle) groups")
    print("-" * 40)
    header = (f"{'file':<24}{'fam':<6}{'idx':<4}{'cyc':<5}{'JULD':>14}"
              f"{'lat':>10}{'lon':>11}{'dir':<4}{'mode':<5}{'lvl':>5}  params")
    for key in pairs:
        print(f"\n(float {key[0]}, cycle {int(key[1])})")
        print(header)
        for record in sorted(groups[key],
                             key=lambda r: (r["family"], r["file"], r["index"])):
            params = ",".join(record["bgc_params"]) or "-"
            juld = "" if record["juld"] is None else f"{record['juld']:.5f}"
            lat = "" if record["lat"] is None else f"{record['lat']:.4f}"
            lon = "" if record["lon"] is None else f"{record['lon']:.4f}"
            print(f"{record['filename']:<24}{record['family']:<6}"
                  f"{record['index']:<4}{int(record['cycle'] or 0):<5}"
                  f"{juld:>14}{lat:>10}{lon:>11}"
                  f"{record['direction'] or '-':<4}"
                  f"{record['data_mode'] or '-':<5}"
                  f"{record['valid_pres_levels']:>5}  {params}")

    print("\n7. Level-by-level comparison of the best candidate")
    print("-" * 40)
    for key in pairs:
        core = [r for r in groups[key] if r["family"] == "core"]
        bgc = [r for r in groups[key] if r["family"] == "bgc"]
        if not core or not bgc:
            continue
        for record in bgc:
            candidates = [c for c in core if signature(c) == signature(record)]
            if not candidates:
                print(f"{record['filename']} (idx {record['index']}): "
                      "no core profile with the same JULD and DIRECTION")
                continue
            best = max(candidates, key=lambda c: c["valid_pres_levels"])
            stats = deep_compare(root, record, best)
            if stats is None:
                continue
            print(f"{record['filename']} idx {record['index']} vs "
                  f"{best['filename']} idx {best['index']}: "
                  f"BGC {stats['bgc_levels']} lvl, core {stats['core_levels']} lvl, "
                  f"{stats['core_levels_found_in_bgc']} core pressures found in BGC, "
                  f"TEMP equal {stats['temp_equal']}/{stats['temp_compared']}")


@functools.lru_cache(maxsize=None)
def cached_var(path, var):
    """Full variable for a file, filled with NaN, cached per file."""
    with netCDF4.Dataset(path, "r") as ds:
        if var not in ds.variables:
            return None
        data = np.ma.filled(ds.variables[var][:].astype("f8"), np.nan)
    return np.atleast_1d(data)


def levels_of(path, var, index):
    data = cached_var(path, var)
    if data is None:
        return None
    if data.ndim == 1:
        return data
    if index >= data.shape[0]:
        return None
    return data[index]


def grid_compare(root, bgc, core):
    """Compare the vertical grids (PRES) of one BGC and one core record."""
    bgc_path = os.path.join(root, bgc["file"])
    core_path = os.path.join(root, core["file"])
    bp = levels_of(bgc_path, "PRES", bgc["index"])
    cp = levels_of(core_path, "PRES", core["index"])
    if bp is None or cp is None:
        return None
    bv = bp[~np.isnan(bp)]
    cv = cp[~np.isnan(cp)]
    if bv.size == 0 or cv.size == 0:
        return None

    bset = {round(float(value), 3) for value in bv}
    cset = {round(float(value), 3) for value in cv}
    shared = len(bset & cset)
    ratio = None
    if np.median(bv) > 0:
        ratio = float(np.median(cv) / np.median(bv))

    if bset == cset:
        shape = "identical_grid"
    elif cset <= bset:
        shape = "core_grid_inside_bgc"
    elif bset <= cset:
        shape = "bgc_grid_inside_core"
    elif shared >= 0.5 * min(len(bset), len(cset)):
        shape = "partial_overlap"
    elif ratio is not None and 500 <= ratio <= 2000:
        shape = "bgc_pres_rescaled"
    else:
        shape = "no_shared_pressure"

    # TEMP/PSAL equality is checked at every shared pressure, not only when the
    # grids are identical: the BGC grid is usually a superset of the core grid.
    temp_equal = psal_equal = compared = 0
    bt = levels_of(bgc_path, "TEMP", bgc["index"])
    ct = levels_of(core_path, "TEMP", core["index"])
    bs = levels_of(bgc_path, "PSAL", bgc["index"])
    cs = levels_of(core_path, "PSAL", core["index"])
    if bt is not None and ct is not None:
        lookup_temp = {}
        lookup_psal = {}
        for position, value in enumerate(bp):
            if np.isnan(value):
                continue
            if position < bt.size and not np.isnan(bt[position]):
                lookup_temp[round(float(value), 3)] = bt[position]
            if bs is not None and position < bs.size \
                    and not np.isnan(bs[position]):
                lookup_psal[round(float(value), 3)] = bs[position]
        for position, value in enumerate(cp):
            if np.isnan(value) or position >= ct.size:
                continue
            key = round(float(value), 3)
            if key in lookup_temp and not np.isnan(ct[position]):
                compared += 1
                if abs(lookup_temp[key] - ct[position]) < 1e-4:
                    temp_equal += 1
                if cs is not None and position < cs.size \
                        and key in lookup_psal and not np.isnan(cs[position]):
                    if abs(lookup_psal[key] - cs[position]) < 1e-4:
                        psal_equal += 1

    return {
        "shape": shape,
        "shared": shared,
        "bgc_n": int(bv.size),
        "core_n": int(cv.size),
        "ratio": ratio,
        "temp_equal": temp_equal,
        "psal_equal": psal_equal,
        "compared": compared,
    }


def report_grids(root, groups, both):
    print("\n8. Vertical-grid agreement for every BGC profile")
    print("-" * 40)
    shapes = collections.Counter()
    per_float_rescaled = collections.Counter()
    rescaled_files = []
    temp_equal = psal_equal = levels_compared = 0
    best_is_max = 0
    no_candidate = 0
    totals = 0

    for key in both:
        core = [r for r in groups[key] if r["family"] == "core"]
        bgc = [r for r in groups[key] if r["family"] == "bgc"]
        for record in bgc:
            totals += 1
            candidates = [c for c in core if signature(c) == signature(record)]
            if not candidates:
                no_candidate += 1
                continue
            compared = [(c, grid_compare(root, record, c)) for c in candidates]
            compared = [(c, s) for c, s in compared if s is not None]
            if not compared:
                no_candidate += 1
                continue
            best_core, best = max(
                compared, key=lambda item: (item[1]["shared"],
                                            item[0]["valid_pres_levels"]))
            shapes[best["shape"]] += 1
            if best["shape"] == "bgc_pres_rescaled":
                per_float_rescaled[key[0]] += 1
                rescaled_files.append(record["filename"])
            temp_equal += best["temp_equal"]
            psal_equal += best["psal_equal"]
            levels_compared += best["compared"]
            max_level = max(candidates, key=lambda c: c["valid_pres_levels"])
            if max_level is best_core:
                best_is_max += 1

    print(f"BGC profiles compared          : {totals}")
    print(f"  no usable core candidate     : {no_candidate}")
    for shape, count in shapes.most_common():
        print(f"  {shape:<26}: {count}")
    print(f"  core candidate with most levels is the best match: "
          f"{best_is_max}/{totals - no_candidate}")
    if levels_compared:
        print(f"  core levels also present in BGC : {levels_compared}")
        print(f"  TEMP equal at those levels      : {temp_equal}")
        print(f"  PSAL equal at those levels      : {psal_equal}")
    if per_float_rescaled:
        print(f"  BGC PRES rescaled (~1000x) by float: "
              f"{dict(per_float_rescaled)}")
        print(f"  affected BGC files: {', '.join(sorted(rescaled_files))}")
    return shapes


def report_core_duplicates(groups):
    print("\n9. Core records that share one physical profile")
    print("-" * 40)
    mode_sets = collections.Counter()
    d_has_fewer = d_has_more = d_equal = 0
    per_signature = collections.Counter()

    for key, members in groups.items():
        by_signature = collections.defaultdict(list)
        for record in members:
            if record["family"] == "core":
                by_signature[signature(record)].append(record)
        for sig, records in by_signature.items():
            per_signature[len(records)] += 1
            modes = tuple(sorted({r["data_mode"] or "-" for r in records}))
            mode_sets[modes] += 1
            delayed = [r for r in records if r["data_mode"] == "D"]
            if delayed:
                others = [r for r in records if r["data_mode"] != "D"]
                if not others:
                    continue
                best_delayed = max(delayed, key=lambda r: r["valid_pres_levels"])
                best_other = max(others, key=lambda r: r["valid_pres_levels"])
                if best_delayed["valid_pres_levels"] > best_other["valid_pres_levels"]:
                    d_has_more += 1
                elif best_delayed["valid_pres_levels"] < best_other["valid_pres_levels"]:
                    d_has_fewer += 1
                else:
                    d_equal += 1

    print(f"records per physical profile  : {dict(sorted(per_signature.items()))}")
    print(f"data-mode sets per profile    : "
          f"{ {str(k): v for k, v in mode_sets.most_common()} }")
    print(f"where a delayed record exists : delayed has more levels {d_has_more}, "
          f"fewer {d_has_fewer}, equal {d_equal}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true",
                        help="Also compare vertical grids across the corpus.")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    root = args.root
    if not os.path.isabs(root):
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), root)
    if not os.path.isdir(root):
        print(f"dataset root not found: {root}", file=sys.stderr)
        return 2

    records, file_count = load_corpus(root)
    groups = group_by_cycle(records)

    report_corpus(records, file_count)
    both = report_groups(groups)
    report_duplication(groups, both)
    report_signature_matching(groups, both)
    report_candidate_ambiguity(groups, both)
    pairs = representative_pairs(groups, both)
    report_pairs(root, groups, pairs)
    if args.deep:
        report_grids(root, groups, both)
        report_core_duplicates(groups)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"files": file_count, "records": records}, handle,
                      indent=1, default=str)
        print(f"\nfull metadata written to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
