import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import xarray as xr
from tqdm import tqdm

from config import ARGO_DATA_DIR
from bgc_matching import (
    UNKNOWN_DIRECTION,
    classify_pressure_grid,
    group_by_cycle,
    index_file,
    plan_group,
    profile_time_from_juld,
)
from database_manager import DatabaseManager


def extract_netcdf_metadata_string(dataset, var_name, profile_idx=0, default="N/A"):
    """
    Extracts and decodes metadata string from NetCDF dataset variables (primary)
    or global attributes (fallback).

    Requirements:
    - Primary source: dataset.variables (case-insensitive lookup)
    - Supports bytes, numpy byte strings, fixed-width character arrays/matrices
    - Decodes UTF-8 and strips trailing spaces, null characters (\\x00), tabs, newlines
    - Supports profile-level indexing through N_PROF (profile_idx)
    - Retains global attribute fallback for non-standard files
    - Returns default ('N/A') when valid metadata cannot be extracted
    """
    # 1. Primary: NetCDF Variables
    for candidate in (var_name, var_name.upper(), var_name.lower()):
        if candidate in dataset.variables:
            var = dataset[candidate]
            raw = var.values
            if raw is None:
                continue

            val = raw
            if hasattr(raw, "ndim") and raw.ndim > 0:
                if profile_idx < len(raw):
                    val = raw[profile_idx]
                else:
                    val = raw[0]

            # Case 1: 1D array of characters/bytes (e.g. character matrix row)
            if isinstance(val, np.ndarray) and val.ndim == 1:
                try:
                    if val.dtype.kind in ("S", "a"):
                        byte_str = b"".join(val.tolist())
                        cleaned = byte_str.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
                    else:
                        str_val = "".join(str(c) for c in val.tolist())
                        cleaned = str_val.strip("\x00 \t\r\n")
                    if cleaned:
                        return cleaned
                except Exception:
                    pass

            # Case 2: Byte scalar or NumPy byte string
            elif isinstance(val, (bytes, np.bytes_)):
                try:
                    cleaned = val.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
                    if cleaned:
                        return cleaned
                except Exception:
                    pass

            # Case 3: 0-dimensional NumPy array or scalar string
            else:
                if hasattr(val, "item") and not isinstance(val, (str, bytes)):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                if isinstance(val, (bytes, np.bytes_)):
                    cleaned = val.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
                else:
                    cleaned = str(val).strip("\x00 \t\r\n")
                if cleaned and cleaned != "None":
                    return cleaned

    # 2. Fallback: Global attributes
    for candidate in (var_name, var_name.lower(), var_name.upper()):
        if candidate in dataset.attrs:
            attr_val = dataset.attrs[candidate]
            if attr_val is not None:
                if isinstance(attr_val, (bytes, np.bytes_)):
                    cleaned = attr_val.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
                else:
                    cleaned = str(attr_val).strip("\x00 \t\r\n")
                if cleaned and cleaned != "None":
                    return cleaned

    return default


def compute_file_sha256(filepath):
    """Computes hexadecimal SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def clean_metadata_str(raw):
    """Decodes bytes/strings and strips nulls, whitespace."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, np.bytes_)):
        s = raw.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
    else:
        s = str(raw).strip("\x00 \t\r\n")
    return s if s and s != "None" else None


def extract_measurement_array(dataset, var_name, profile_idx=0):
    """
    Extracts numerical measurement array from dataset.
    Converts NetCDF fill values (99999.0, 60000.0, etc.) and NaN to None.
    """
    if var_name not in dataset.variables:
        return None
    var = dataset[var_name]
    vals = var.values
    if vals is None or len(vals) == 0:
        return None
    if hasattr(vals, "ndim") and vals.ndim > 1:
        row = vals[profile_idx] if profile_idx < len(vals) else vals[0]
    else:
        row = vals
    res = []
    for x in np.asarray(row).flatten():
        if x is None or np.isnan(x) or x in (99999.0, 60000.0, -99999.0):
            res.append(None)
        else:
            res.append(round(float(x), 4))
    return res


def extract_qc_array(dataset, var_name, profile_idx=0):
    """
    Extracts character QC array for measurements.
    Preserves measurement-level flags '0'-'9'.
    Normalizes blanks, spaces, and fill values to None.
    """
    if var_name not in dataset.variables:
        return None
    var = dataset[var_name]
    vals = var.values
    if vals is None or len(vals) == 0:
        return None
    if hasattr(vals, "ndim") and vals.ndim > 1:
        row = vals[profile_idx] if profile_idx < len(vals) else vals[0]
    else:
        row = vals
    res = []
    for c in np.asarray(row).flatten():
        if isinstance(c, (bytes, np.bytes_)):
            s = c.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
        else:
            s = str(c).strip("\x00 \t\r\n")
        if s in {"0", "1", "2", "3", "4", "5", "8", "9"}:
            res.append(s)
        else:
            res.append(None)
    return res


def _profile_scalar_value(raw, profile_idx=0):
    """
    Selects a single value from either a profile-dimensioned array or a
    scalar (0-D) array.

    ``xarray`` drops the ``N_PROF`` dimension when a dataset is subset with
    ``isel(N_PROF=i)``, so profile-level character variables such as
    ``DATA_MODE`` and ``PROFILE_PRES_QC`` arrive here as 0-D arrays.
    Calling ``len()`` on those raises ``TypeError: len() of unsized object``,
    which previously aborted ingestion for every profile.

    An index that is negative or beyond the available profiles yields None.
    Another profile's value is never substituted for a missing one.
    """
    if raw is None:
        return None

    arr = np.asarray(raw)

    if arr.ndim == 0:
        return arr.item()

    if arr.size == 0:
        return None

    if 0 <= profile_idx < arr.shape[0]:
        return arr[profile_idx]

    return None


def extract_profile_qc(dataset, var_name, profile_idx=0):
    """
    Extracts profile-level QC flag.
    Valid domain: ADMT letters 'A'-'F' (or None).
    Accepts both profile-dimensioned arrays and the 0-D scalars produced by
    ``isel(N_PROF=...)``.
    """
    if var_name not in dataset.variables:
        return None

    item = _profile_scalar_value(dataset[var_name].values, profile_idx)
    s = clean_metadata_str(item)

    if s and s.upper() in {"A", "B", "C", "D", "E", "F"}:
        return s.upper()
    return None


def extract_data_mode(dataset, profile_idx=0):
    """
    Extracts single-character processing DATA_MODE ('D', 'R', 'A') or None.
    Accepts both profile-dimensioned arrays and the 0-D scalars produced by
    ``isel(N_PROF=...)``.
    """
    if "DATA_MODE" not in dataset.variables:
        return None

    item = _profile_scalar_value(dataset["DATA_MODE"].values, profile_idx)
    s = clean_metadata_str(item)

    return s[0].upper() if s else None


def extract_scientific_calib(dataset, profile_idx=0):
    """
    Extracts scientific calibration records preserving N_PROF, N_CALIB, N_PARAM dimensions.
    """
    if "SCIENTIFIC_CALIB_EQUATION" not in dataset.variables:
        return None

    eq_var = dataset["SCIENTIFIC_CALIB_EQUATION"]
    coef_var = dataset.get("SCIENTIFIC_CALIB_COEFFICIENT")
    com_var = dataset.get("SCIENTIFIC_CALIB_COMMENT")
    date_var = dataset.get("SCIENTIFIC_CALIB_DATE")
    param_var = dataset.get("PARAMETER")

    n_prof = dataset.sizes.get("N_PROF", 1)
    n_calib = dataset.sizes.get("N_CALIB", 1)
    n_param = dataset.sizes.get("N_PARAM", 1)

    calibs = []
    p_idx = profile_idx if profile_idx < n_prof else 0

    for c_idx in range(n_calib):
        for m_idx in range(n_param):
            try:
                param_name = clean_metadata_str(param_var.values[p_idx, c_idx, m_idx]) if param_var is not None else None
                equation = clean_metadata_str(eq_var.values[p_idx, c_idx, m_idx])
                coefficient = clean_metadata_str(coef_var.values[p_idx, c_idx, m_idx]) if coef_var is not None else None
                comment = clean_metadata_str(com_var.values[p_idx, c_idx, m_idx]) if com_var is not None else None
                date_str = clean_metadata_str(date_var.values[p_idx, c_idx, m_idx]) if date_var is not None else None

                if any([param_name, equation, coefficient, comment, date_str]):
                    calibs.append({
                        "profile_index": p_idx,
                        "calibration_index": c_idx,
                        "parameter": param_name,
                        "equation": equation,
                        "coefficient": coefficient,
                        "comment": comment,
                        "date": date_str,
                    })
            except Exception:
                continue

    return calibs if calibs else None


def _profile_row(values, profile_idx=0):
    """
    Returns the row belonging to one profile.

    Callers either pass a full dataset (N_PROF, ...) or a dataset already subset
    with ``isel(N_PROF=...)``, where the N_PROF axis is gone. For a 1-D array the
    remaining axis is the per-profile axis (N_PARAM / N_LEVELS), because the
    profile has already been selected by the caller.
    """
    array = np.asarray(values)

    if array.ndim <= 1:
        return array

    if 0 <= profile_idx < array.shape[0]:
        return array[profile_idx]

    return array[0]


def _station_parameter_names(dataset, profile_idx=0):
    """Parameter names for one profile, tolerating an already-subset dataset."""
    if "STATION_PARAMETERS" not in dataset.variables:
        return []

    row = _profile_row(dataset["STATION_PARAMETERS"].values, profile_idx)

    names = []
    for item in np.atleast_1d(row):
        name = clean_metadata_str(item)
        if name:
            names.append(name.upper())
    return names


def _parameter_data_modes(dataset, profile_idx=0):
    """
    Maps parameter name -> PARAMETER_DATA_MODE flag for one profile.

    The mode is read from PARAMETER_DATA_MODE only. It is never derived from the
    profile-level DATA_MODE, and unresolved metadata is left out rather than
    guessed.
    """
    if "PARAMETER_DATA_MODE" not in dataset.variables:
        return {}

    names = _station_parameter_names(dataset, profile_idx)
    if not names:
        return {}

    modes = np.atleast_1d(
        _profile_row(dataset["PARAMETER_DATA_MODE"].values, profile_idx)
    )

    # Positional association is only trustworthy when both axes describe the
    # same parameters; otherwise the metadata stays unresolved.
    if len(modes) != len(names):
        return {}

    result = {}
    for name, raw_mode in zip(names, modes):
        mode = clean_metadata_str(raw_mode)
        if name and mode:
            result[name] = mode.upper()
    return result


# Core-Argo auxiliary and derived variables. They appear in the same
# STATION_PARAMETERS list as the core measurements, but they are derived from
# PRES/TEMP/PSAL (running means, standard deviations) or are a time offset, not
# biogeochemical measurements, so dynamic BGC discovery must skip them.
CORE_AUXILIARY_PARAMETERS = frozenset({"MTIME"})
CORE_AUXILIARY_SUFFIXES = ("_STD", "_MED", "_dPRES")


def is_core_auxiliary_parameter(name):
    """True for a core-Argo auxiliary/derived parameter, never for a BGC one."""
    if not name:
        return False
    return (
        name in CORE_AUXILIARY_PARAMETERS
        or name.endswith(CORE_AUXILIARY_SUFFIXES)
    )


def extract_profile_position(profile_ds):
    """
    Returns ``(latitude, longitude)``, using None for a missing position fix.

    Argo leaves LATITUDE/LONGITUDE at NaN (occasionally a fill value) when a
    profile has no GPS fix. That means "unknown", so it must become SQL NULL:
    ``float()`` alone passes NaN through to MySQL, which used to fail the NOT
    NULL constraint and abort the whole file. A missing coordinate is never
    replaced by 0 or by another profile's position.
    """
    return (
        _position_component(profile_ds, "LATITUDE"),
        _position_component(profile_ds, "LONGITUDE"),
    )


def _position_component(profile_ds, var_name):
    if var_name not in profile_ds.variables:
        return None
    values = np.asarray(profile_ds[var_name].values, dtype="float64").reshape(-1)
    if values.size == 0:
        return None
    value = float(values[0])
    if np.isnan(value) or value in (99999.0, -99999.0):
        return None
    return value


def extract_bgc_data(dataset, profile_idx=0, metadata_dataset=None,
                     metadata_profile_idx=None):
    """
    Dynamically extracts bio-geochemical measurements, quality flags, adjusted values,
    and parameter data modes where available.

    Parameter-level modes are extracted independently of the BGC measurements, so a
    dataset without BGC arrays still yields its PARAMETER_DATA_MODE metadata.

    ``metadata_dataset`` optionally supplies the full multi-profile dataset for
    metadata alignment; ``_process_single_profile`` passes it because ``dataset``
    has already been subset with ``isel(N_PROF=...)``.
    """
    core_params = {"PRES", "TEMP", "PSAL"}
    known_bgc = ["DOXY", "CHLA", "BBP700", "NITRATE", "PH_IN_SITU_TOTAL"]

    metadata_source = metadata_dataset if metadata_dataset is not None else dataset
    metadata_index = (
        metadata_profile_idx if metadata_profile_idx is not None else profile_idx
    )
    param_data_mode = _parameter_data_modes(metadata_source, metadata_index)

    discovered = [p for p in known_bgc if p in dataset.variables]

    for name in _station_parameter_names(dataset, profile_idx):
        if name in core_params or name in discovered:
            continue
        # MTIME / *_STD / *_MED / *_dPRES belong to the core product.
        # Storing them as BGC would add ~19 KB of pseudo-BGC to every
        # core profile and mislabel derived values as measurements.
        if is_core_auxiliary_parameter(name):
            continue
        if name in dataset.variables:
            discovered.append(name)

    if not discovered:
        return (
            None, None, None, None, None,
            param_data_mode if param_data_mode else None,
        )

    bgc_params = {}
    bgc_qc = {}
    bgc_adj = {}
    bgc_adj_err = {}
    bgc_adj_qc = {}

    for var in discovered:
        vals = extract_measurement_array(dataset, var, profile_idx)
        if vals is not None:
            bgc_params[var] = vals
            bgc_qc[var] = extract_qc_array(dataset, f"{var}_QC", profile_idx)
            bgc_adj[var] = extract_measurement_array(dataset, f"{var}_ADJUSTED", profile_idx)
            bgc_adj_err[var] = extract_measurement_array(dataset, f"{var}_ADJUSTED_ERROR", profile_idx)
            bgc_adj_qc[var] = extract_qc_array(dataset, f"{var}_ADJUSTED_QC", profile_idx)

    return (
        bgc_params if bgc_params else None,
        bgc_qc if bgc_qc else None,
        bgc_adj if bgc_adj else None,
        bgc_adj_err if bgc_adj_err else None,
        bgc_adj_qc if bgc_adj_qc else None,
        param_data_mode if param_data_mode else None,
    )


def extract_source_metadata(dataset, filepath, profile_idx=0):
    """Extracts provenance metadata dictionary from NetCDF attributes/variables."""
    filename = os.path.basename(filepath)
    prefix = filename[:2].upper() if len(filename) >= 2 else ""
    if prefix.startswith("SD"):
        product_type = "synthetic_delayed"
    elif prefix.startswith("SR"):
        product_type = "synthetic_realtime"
    elif prefix.startswith("BD"):
        product_type = "bgc_delayed"
    elif prefix.startswith("BR"):
        product_type = "bgc_realtime"
    elif prefix.startswith("D"):
        product_type = "core_delayed"
    elif prefix.startswith("R"):
        product_type = "core_realtime"
    else:
        product_type = "unknown"

    return {
        "product_type": product_type,
        "source_file_path": filepath,
        "data_centre": extract_netcdf_metadata_string(dataset, "DATA_CENTRE", profile_idx, default="N/A"),
        "format_version": extract_netcdf_metadata_string(dataset, "FORMAT_VERSION", profile_idx, default="N/A"),
        "date_creation": extract_netcdf_metadata_string(dataset, "DATE_CREATION", profile_idx, default="N/A"),
        "date_update": extract_netcdf_metadata_string(dataset, "DATE_UPDATE", profile_idx, default="N/A"),
        "ingestion_timestamp": datetime.now(timezone.utc).isoformat(),
    }


def extract_direction(dataset):
    """
    Extracts the profile DIRECTION ('A' ascending, 'D' descending) or None.

    Direction is part of the logical profile identity, so a value outside the
    Argo domain is reported as unknown (None) instead of being stored as though
    it were a direction.
    """
    if "DIRECTION" not in dataset.variables:
        return None

    item = _profile_scalar_value(dataset["DIRECTION"].values, 0)
    s = clean_metadata_str(item)

    if s and s[0].upper() in {"A", "D"}:
        return s[0].upper()
    return None


class ArgoDataProcessor:
    """Ingest ARGO NetCDF products into MySQL and ChromaDB together.

    Ingestion is grouped by ``(float, cycle)``: the core and BGC products of one
    cycle are indexed first, matched on ``(JULD, DIRECTION)``, and written as one
    row per logical profile (``docs/bgc/BGC_PRODUCT_MATCHING.md``). The BGC
    measurements keep their own pressure axis; nothing is interpolated or
    index-aligned across products.
    """

    # Copied from the BGC product onto the merged row when a core profile is
    # present. The core's own pressure/temperature/salinity columns are never
    # replaced by the BGC ones.
    BGC_ATTACH_FIELDS = (
        "bgc_params",
        "bgc_qc",
        "bgc_adjusted",
        "bgc_adjusted_error",
        "bgc_adjusted_qc",
        "parameter_data_mode",
    )

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        # Logical identities written during one top-level run. A second row for
        # the same identity would be a fabricated association, so it is reported
        # instead of inserted.
        self._identities_in_run = set()

    # ---------------------------------------------------------
    # PROFILE PAYLOAD
    # ---------------------------------------------------------

    def _build_profile_payload(
        self,
        profile_ds,
        float_id_db,
        nc_file_path=None,
        file_sha256=None,
        profile_index=0,
        full_dataset=None,
        direction=None,
        core_variants=None,
    ):
        """
        Builds the argo_profiles row for one profile, without writing it.

        The same builder is used for a core record, a BGC record and a merged
        row, so every column is produced by one code path.
        """

        cycle_number = int(profile_ds["CYCLE_NUMBER"].values)
        profile_time = profile_time_from_juld(
            float(np.atleast_1d(profile_ds["JULD"].values)[0])
        )
        latitude, longitude = extract_profile_position(profile_ds)

        if direction is None:
            direction = extract_direction(profile_ds)
        direction = direction or UNKNOWN_DIRECTION

        # 1. Core Measurements (Raw, QC, Adjusted, Error, Adjusted QC)
        pressure = extract_measurement_array(profile_ds, "PRES")
        pressure_qc = extract_qc_array(profile_ds, "PRES_QC")
        pressure_adjusted = extract_measurement_array(profile_ds, "PRES_ADJUSTED")
        pressure_adjusted_error = extract_measurement_array(profile_ds, "PRES_ADJUSTED_ERROR")
        pressure_adjusted_qc = extract_qc_array(profile_ds, "PRES_ADJUSTED_QC")

        temperature = extract_measurement_array(profile_ds, "TEMP")
        temperature_qc = extract_qc_array(profile_ds, "TEMP_QC")
        temperature_adjusted = extract_measurement_array(profile_ds, "TEMP_ADJUSTED")
        temperature_adjusted_error = extract_measurement_array(profile_ds, "TEMP_ADJUSTED_ERROR")
        temperature_adjusted_qc = extract_qc_array(profile_ds, "TEMP_ADJUSTED_QC")

        salinity = extract_measurement_array(profile_ds, "PSAL")
        salinity_qc = extract_qc_array(profile_ds, "PSAL_QC")
        salinity_adjusted = extract_measurement_array(profile_ds, "PSAL_ADJUSTED")
        salinity_adjusted_error = extract_measurement_array(profile_ds, "PSAL_ADJUSTED_ERROR")
        salinity_adjusted_qc = extract_qc_array(profile_ds, "PSAL_ADJUSTED_QC")

        # 2. Profile-Level Processing Mode and Quality Flags
        data_mode = extract_data_mode(profile_ds)
        profile_pres_qc = extract_profile_qc(profile_ds, "PROFILE_PRES_QC")
        profile_temp_qc = extract_profile_qc(profile_ds, "PROFILE_TEMP_QC")
        profile_psal_qc = extract_profile_qc(profile_ds, "PROFILE_PSAL_QC")

        # 3. Scientific Calibration
        calib_dataset = full_dataset if full_dataset is not None else profile_ds
        scientific_calib = extract_scientific_calib(calib_dataset, profile_index)

        # 4. Dynamic BGC Parameters
        (
            bgc_params,
            bgc_qc,
            bgc_adjusted,
            bgc_adjusted_error,
            bgc_adjusted_qc,
            parameter_data_mode,
        ) = extract_bgc_data(
            profile_ds,
            profile_index,
            metadata_dataset=full_dataset,
        )

        # 5. Provenance Metadata
        source_file = os.path.basename(nc_file_path) if nc_file_path else None
        source_metadata = (
            extract_source_metadata(profile_ds, nc_file_path, profile_index)
            if nc_file_path
            else None
        )
        if source_metadata is not None and core_variants is not None:
            source_metadata["core_variants"] = core_variants

        # 6. BGC provenance and the BGC product's own pressure axis. The axis is
        # stored whenever BGC measurements are, because a BGC array is only
        # interpretable together with the pressure it was delivered with.
        bgc_pressure = (
            json.dumps(pressure) if bgc_params is not None and pressure is not None
            else None
        )
        bgc_source_metadata = None
        if bgc_params is not None:
            bgc_source_metadata = {
                "source_file": source_file,
                "source_file_sha256": file_sha256,
                "profile_index_in_file": profile_index,
                "product_type": (
                    source_metadata["product_type"] if source_metadata else None
                ),
                "data_mode": data_mode,
                "direction": direction,
                "juld": float(np.atleast_1d(profile_ds["JULD"].values)[0]),
                "level_count": len(pressure) if pressure else 0,
                "match": None,
                "selection_rule": None,
                "grid_check": None,
                "grid_check_detail": None,
                "candidates": [],
                "position_used_for_profile": False,
                "profile_position": {"latitude": latitude, "longitude": longitude},
            }

        return {
            "float_id": float_id_db,
            "cycle_number": cycle_number,
            "direction": direction,
            "profile_time": profile_time,
            "latitude": latitude,
            "longitude": longitude,
            # Core Raw
            "pressure": json.dumps(pressure) if pressure is not None else None,
            "temperature": json.dumps(temperature) if temperature is not None else None,
            "salinity": json.dumps(salinity) if salinity is not None else None,
            # Core QC
            "pressure_qc": json.dumps(pressure_qc) if pressure_qc is not None else None,
            "temperature_qc": json.dumps(temperature_qc) if temperature_qc is not None else None,
            "salinity_qc": json.dumps(salinity_qc) if salinity_qc is not None else None,
            # Core Adjusted
            "pressure_adjusted": json.dumps(pressure_adjusted) if pressure_adjusted is not None else None,
            "temperature_adjusted": json.dumps(temperature_adjusted) if temperature_adjusted is not None else None,
            "salinity_adjusted": json.dumps(salinity_adjusted) if salinity_adjusted is not None else None,
            # Core Adjusted Error
            "pressure_adjusted_error": json.dumps(pressure_adjusted_error) if pressure_adjusted_error is not None else None,
            "temperature_adjusted_error": json.dumps(temperature_adjusted_error) if temperature_adjusted_error is not None else None,
            "salinity_adjusted_error": json.dumps(salinity_adjusted_error) if salinity_adjusted_error is not None else None,
            # Core Adjusted QC
            "pressure_adjusted_qc": json.dumps(pressure_adjusted_qc) if pressure_adjusted_qc is not None else None,
            "temperature_adjusted_qc": json.dumps(temperature_adjusted_qc) if temperature_adjusted_qc is not None else None,
            "salinity_adjusted_qc": json.dumps(salinity_adjusted_qc) if salinity_adjusted_qc is not None else None,
            # Profile Metadata
            "data_mode": data_mode,
            "profile_pres_qc": profile_pres_qc,
            "profile_temp_qc": profile_temp_qc,
            "profile_psal_qc": profile_psal_qc,
            # Calibration & BGC
            "scientific_calib": json.dumps(scientific_calib) if scientific_calib is not None else None,
            "parameter_data_mode": json.dumps(parameter_data_mode) if parameter_data_mode is not None else None,
            "bgc_params": json.dumps(bgc_params) if bgc_params is not None else None,
            "bgc_qc": json.dumps(bgc_qc) if bgc_qc is not None else None,
            "bgc_adjusted": json.dumps(bgc_adjusted) if bgc_adjusted is not None else None,
            "bgc_adjusted_error": json.dumps(bgc_adjusted_error) if bgc_adjusted_error is not None else None,
            "bgc_adjusted_qc": json.dumps(bgc_adjusted_qc) if bgc_adjusted_qc is not None else None,
            "bgc_pressure": bgc_pressure,
            "bgc_source_metadata": (
                json.dumps(bgc_source_metadata)
                if bgc_source_metadata is not None else None
            ),
            # Provenance
            "source_file": source_file,
            "source_file_sha256": file_sha256,
            "profile_index_in_file": profile_index,
            "source_metadata": json.dumps(source_metadata) if source_metadata is not None else None,
        }

    # ---------------------------------------------------------
    # WRITE ONE LOGICAL PROFILE
    # ---------------------------------------------------------

    def _reconcile_existing_vector(self, identity):
        """
        Rebuilds the vector of an already stored profile when it is missing.

        MySQL commits a row before its ChromaDB vector is written, so a crash
        in between leaves a row that a re-ingestion would otherwise skip
        forever. The relational row is authoritative; an existing vector that
        still matches it is left untouched.
        """

        profile_id = self.db_manager.get_profile_id(*identity)

        if profile_id is None:
            # The row vanished between the existence check and the lookup;
            # nothing was stored, and the next run will handle it.
            return

        status = self.db_manager.sync_profile_vector(profile_id)

        if status != "unchanged":
            logging.warning(
                "Profile %s was already stored; its ChromaDB vector was %s "
                "from the relational row.", profile_id, status)

    def _store_payload(self, payload, float_id, guard=True):
        """Writes one logical profile, or reports that it is already stored.

        ``guard`` is on for group ingestion, where two records resolving to the
        same identity would mean the matcher produced a duplicate; the
        single-profile entry point leaves it off because it is not a run.
        """

        identity = (
            payload["float_id"],
            payload["cycle_number"],
            payload["direction"],
            payload["profile_time"],
        )

        if guard:
            if identity in self._identities_in_run:
                raise RuntimeError(
                    "two profiles of this run resolve to the same logical "
                    f"identity {identity}; refusing to store one as if it were "
                    "the other"
                )
            self._identities_in_run.add(identity)

        if self.db_manager.check_profile_exists(*identity):
            self._reconcile_existing_vector(identity)
            return "skipped"

        profile_id_db = self.db_manager.insert_profile(payload)

        # The vector is built from the committed row, not from the payload:
        # that is the same source reconciliation reads, so a fresh write and a
        # later repair of it produce identical vectors.
        self.db_manager.add_profile_from_row(profile_id_db)

        return "inserted"

    def _process_single_profile(
        self,
        profile_ds,
        float_id_db,
        float_id,
        nc_file_path=None,
        file_sha256=None,
        profile_index=0,
        full_dataset=None,
    ):
        """Builds and stores one profile on its own (no core/BGC matching)."""

        payload = self._build_profile_payload(
            profile_ds,
            float_id_db,
            nc_file_path=nc_file_path,
            file_sha256=file_sha256,
            profile_index=profile_index,
            full_dataset=full_dataset,
        )
        return self._store_payload(payload, float_id, guard=False)

    # ---------------------------------------------------------
    # GROUP INGESTION
    # ---------------------------------------------------------

    @staticmethod
    def _file_sha256(path, cache):
        if path not in cache:
            cache[path] = compute_file_sha256(path)
        return cache[path]

    def _write_plan(self, plan, datasets, hashes, float_id_db, float_id):
        """Writes one planned logical profile (core, BGC or merged)."""

        core_record = plan["core"]
        bgc_record = plan["bgc"]
        base = core_record or bgc_record
        if base is None:
            raise ValueError("plan has neither a core nor a BGC record")

        dataset = datasets[base["path"]]
        payload = self._build_profile_payload(
            dataset.isel(N_PROF=base["index"]),
            float_id_db,
            nc_file_path=base["path"],
            file_sha256=self._file_sha256(base["path"], hashes),
            profile_index=base["index"],
            full_dataset=dataset,
            direction=plan["direction"],
            core_variants=plan["core_variants"] or None,
        )

        core_pressure = (
            json.loads(payload["pressure"]) if core_record and payload["pressure"]
            else None
        )
        position_from_bgc = False

        if bgc_record is not None:
            bgc_dataset = datasets[bgc_record["path"]]
            bgc_payload = self._build_profile_payload(
                bgc_dataset.isel(N_PROF=bgc_record["index"]),
                float_id_db,
                nc_file_path=bgc_record["path"],
                file_sha256=self._file_sha256(bgc_record["path"], hashes),
                profile_index=bgc_record["index"],
                full_dataset=bgc_dataset,
                direction=plan["direction"],
            )

            for field in self.BGC_ATTACH_FIELDS:
                payload[field] = bgc_payload[field]
            payload["bgc_pressure"] = bgc_payload["bgc_pressure"]
            payload["bgc_source_metadata"] = bgc_payload["bgc_source_metadata"]

            # The core profile is authoritative for its position. Only when it
            # has no fix at all is the matched BGC profile's position used — the
            # same physical profile, never a derived or invented coordinate.
            position_from_bgc = (
                core_record is not None
                and payload["latitude"] is None
                and bgc_payload["latitude"] is not None
            )
            if position_from_bgc:
                payload["latitude"] = bgc_payload["latitude"]
                payload["longitude"] = bgc_payload["longitude"]

        if payload["bgc_source_metadata"] is not None:
            metadata = json.loads(payload["bgc_source_metadata"])
            metadata["match"] = plan["match"]
            metadata["selection_rule"] = plan["core_selection_rule"]
            metadata["candidates"] = plan["bgc_candidates"]
            metadata["position_used_for_profile"] = position_from_bgc
            grid_status, grid_detail = classify_pressure_grid(
                json.loads(payload["bgc_pressure"]) if payload["bgc_pressure"] else None,
                core_pressure if core_record is not None else None,
            )
            metadata["grid_check"] = grid_status
            metadata["grid_check_detail"] = grid_detail
            payload["bgc_source_metadata"] = json.dumps(metadata)

        return self._store_payload(payload, float_id)

    def process_group(self, float_wmo, cycle_number, core_records, bgc_records):
        """
        Ingests one ``(float, cycle)`` group: every core variant and every BGC
        record of that cycle becomes exactly one row per logical profile.
        """

        result = {
            "wmo_number": float_wmo,
            "cycle_number": cycle_number,
            "inserted": 0,
            "skipped": 0,
            "errors": [],
        }

        plans = plan_group(core_records, bgc_records)
        if not plans:
            return result

        datasets = {}
        hashes = {}
        try:
            for record in core_records + bgc_records:
                path = record["path"]
                if path in datasets:
                    continue
                try:
                    datasets[path] = xr.open_dataset(path, decode_times=False)
                except Exception as error:
                    # The profiles of this file cannot be read; the rest of the
                    # group still is, and the failure is reported.
                    logging.warning(
                        "Could not open %s: %s", os.path.basename(path), error)
                    result["errors"].append({
                        "file": os.path.basename(path),
                        "error": str(error),
                    })
                    return result

            first_dataset = next(iter(datasets.values()))
            project_name = extract_netcdf_metadata_string(
                first_dataset, "PROJECT_NAME", default="N/A")
            platform_type = extract_netcdf_metadata_string(
                first_dataset, "PLATFORM_TYPE", default="N/A")
            float_id_db = self.db_manager.get_or_create_float(
                int(float_wmo), project_name, platform_type)

            for plan in plans:
                try:
                    status = self._write_plan(
                        plan, datasets, hashes, float_id_db, str(float_wmo))
                except Exception as error:
                    logging.warning(
                        "Could not ingest %s cycle %s (%s @ %s): %s",
                        float_wmo, cycle_number, plan["direction"],
                        plan["profile_time"], error,
                    )
                    result["errors"].append({
                        "file": (
                            (plan["core"] or plan["bgc"])["file"]
                        ),
                        "error": f"cycle {cycle_number} profile "
                                 f"{plan['direction']} @ {plan['profile_time']}: "
                                 f"{error}",
                    })
                    continue
                result[status] += 1

        finally:
            for dataset in datasets.values():
                try:
                    dataset.close()
                except Exception:
                    pass

        return result

    # ---------------------------------------------------------
    # DATASET INGESTION
    # ---------------------------------------------------------

    def process_paths(self, paths, progress=False):
        """
        Indexes the given files, groups them by ``(float, cycle)`` and ingests
        one row per logical profile. Safe to call again on the same files.
        """

        paths = [os.fspath(path) for path in paths]
        summary = {
            "files": len(paths),
            "groups": 0,
            "inserted": 0,
            "skipped": 0,
            "errors": [],
        }
        self._identities_in_run = set()

        records = []
        for path in paths:
            try:
                records.extend(index_file(path))
            except Exception as error:
                logging.warning(
                    "Could not index %s: %s", os.path.basename(path), error)
                summary["errors"].append({
                    "file": os.path.basename(path),
                    "error": str(error),
                })

        usable = []
        for record in records:
            if record["float"] is None or record["cycle"] is None:
                summary["errors"].append({
                    "file": record["file"],
                    "error": f"N_PROF {record['index']} has no usable "
                             "PLATFORM_NUMBER/CYCLE_NUMBER and was not stored",
                })
            else:
                usable.append(record)

        groups = group_by_cycle(usable)
        summary["groups"] = len(groups)

        keys = sorted(groups)
        iterator = (
            tqdm(keys, desc="Processing (float, cycle) groups") if progress else keys
        )
        for key in iterator:
            group = groups[key]
            try:
                result = self.process_group(
                    key[0], key[1], group["core"], group["bgc"])
            except Exception as error:
                # One unusable group must not abort the whole run, and must not
                # be reported as success either.
                logging.warning(
                    "Could not ingest float %s cycle %s: %s", key[0], key[1], error)
                summary["errors"].append({
                    "file": str(key[0]),
                    "error": f"cycle {key[1]}: {error}",
                })
                continue
            summary["inserted"] += result["inserted"]
            summary["skipped"] += result["skipped"]
            summary["errors"].extend(result["errors"])

        return summary

    def process_dataset(self, directory, recursive=True, progress=False):
        """Ingests every NetCDF file under ``directory`` as one corpus."""

        if not os.path.isdir(directory):
            raise FileNotFoundError(f"ARGO data directory does not exist: {directory}")

        paths = []
        for root, _directories, filenames in os.walk(directory):
            for filename in filenames:
                if filename.lower().endswith(".nc"):
                    paths.append(os.path.join(root, filename))
            if not recursive:
                break

        return self.process_paths(sorted(paths), progress=progress)

    def process_and_ingest(self, directory=ARGO_DATA_DIR):
        """Ingests the flat NetCDF directory configured by ``ARGO_DATA_DIR``."""

        return self.process_dataset(directory, recursive=False, progress=True)

    def process_file(self, nc_file_path):
        """Ingests one saved upload; safe to call again for an existing file."""

        summary = self.process_paths([nc_file_path])
        errors = [
            f"{entry['file']}: {entry['error']}" for entry in summary["errors"]
        ]
        return {
            "filename": os.path.basename(nc_file_path),
            "inserted": summary["inserted"],
            "skipped": summary["skipped"],
            "error": "; ".join(errors) if errors else None,
            "errors": errors,
            "groups": summary["groups"],
        }
