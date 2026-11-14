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


def extract_bgc_data(dataset, profile_idx=0):
    """
    Dynamically extracts bio-geochemical measurements, quality flags, adjusted values,
    and parameter data modes where available.
    """
    core_params = {"PRES", "TEMP", "PSAL"}
    known_bgc = ["DOXY", "CHLA", "BBP700", "NITRATE", "PH_IN_SITU_TOTAL"]
    discovered = [p for p in known_bgc if p in dataset.variables]

    if "STATION_PARAMETERS" in dataset.variables:
        raw_sp = dataset["STATION_PARAMETERS"].values
        row_sp = raw_sp[profile_idx] if profile_idx < len(raw_sp) else raw_sp[0]
        for item in row_sp:
            name = item.decode().strip() if isinstance(item, bytes) else str(item).strip()
            if name and name not in core_params and name not in discovered:
                if name in dataset.variables:
                    discovered.append(name)

    if not discovered:
        return None, None, None, None, None, None

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

    param_data_mode = {}
    if "PARAMETER_DATA_MODE" in dataset.variables:
        pdm_vals = dataset["PARAMETER_DATA_MODE"].values
        row = pdm_vals[profile_idx] if profile_idx < len(pdm_vals) else pdm_vals[0]
        if "STATION_PARAMETERS" in dataset.variables:
            raw_sp = dataset["STATION_PARAMETERS"].values
            row_sp = raw_sp[profile_idx] if profile_idx < len(raw_sp) else raw_sp[0]
            for i, p_name in enumerate(row_sp):
                p_str = p_name.decode().strip() if isinstance(p_name, bytes) else str(p_name).strip()
                if i < len(row):
                    m_char = row[i].decode().strip() if isinstance(row[i], bytes) else str(row[i]).strip()
                    if p_str and m_char:
                        param_data_mode[p_str] = m_char

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


class ArgoDataProcessor:
    """Ingest ARGO NetCDF files into MySQL and ChromaDB together."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def process_and_ingest(self, directory=ARGO_DATA_DIR):
        """Ingest every NetCDF file in ``directory`` and return a summary."""

        if not os.path.isdir(directory):
            raise FileNotFoundError(f"ARGO data directory does not exist: {directory}")

        nc_files = sorted(
            os.path.join(directory, filename)
            for filename in os.listdir(directory)
            if filename.lower().endswith(".nc")
        )
        summary = {"files": len(nc_files), "inserted": 0, "skipped": 0, "errors": []}

        for nc_file_path in tqdm(nc_files, desc="Processing ARGO files"):
            result = self.process_file(nc_file_path)
            summary["inserted"] += result["inserted"]
            summary["skipped"] += result["skipped"]
            if result["error"]:
                summary["errors"].append(result)

        return summary

    def process_file(self, nc_file_path):
        """Ingest one saved upload; safe to call again for an existing file."""

        result = {
            "filename": os.path.basename(nc_file_path),
            "inserted": 0,
            "skipped": 0,
            "error": None,
        }

        try:
            file_sha256 = compute_file_sha256(nc_file_path)
            with xr.open_dataset(nc_file_path, decode_times=False) as dataset:
                raw_wmo = dataset["PLATFORM_NUMBER"].values[0]
                wmo_number = int(
                    raw_wmo.decode().strip()
                    if isinstance(raw_wmo, bytes)
                    else str(raw_wmo).strip()
                )
                project_name = extract_netcdf_metadata_string(dataset, "PROJECT_NAME", default="N/A")
                platform_type = extract_netcdf_metadata_string(dataset, "PLATFORM_TYPE", default="N/A")
                float_id_db = self.db_manager.get_or_create_float(
                    wmo_number,
                    project_name,
                    platform_type,
                )

                for index in range(dataset.sizes["N_PROF"]):
                    status = self._process_single_profile(
                        dataset.isel(N_PROF=index),
                        float_id_db,
                        str(wmo_number),
                        nc_file_path=nc_file_path,
                        file_sha256=file_sha256,
                        profile_index=index,
                        full_dataset=dataset,
                    )
                    result[status] += 1

        except Exception as error:
            logging.warning("Could not ingest %s: %s", result["filename"], error)
            result["error"] = str(error)

        return result

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
        """Insert one profile in both stores, or report an existing profile."""

        cycle_number = int(profile_ds["CYCLE_NUMBER"].values)
        if self.db_manager.check_profile_exists(float_id_db, cycle_number):
            return "skipped"

        profile_time = pd.Timestamp("1950-01-01") + pd.to_timedelta(
            profile_ds["JULD"].values, unit="D"
        )
        latitude = float(profile_ds["LATITUDE"].values)
        longitude = float(profile_ds["LONGITUDE"].values)

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
        ) = extract_bgc_data(profile_ds)

        # 5. Provenance Metadata
        source_file = os.path.basename(nc_file_path) if nc_file_path else None
        source_metadata = (
            extract_source_metadata(profile_ds, nc_file_path, profile_index)
            if nc_file_path
            else None
        )

        profile_data = {
            "float_id": float_id_db,
            "cycle_number": cycle_number,
            "profile_time": profile_time.to_pydatetime(),
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
            # Provenance
            "source_file": source_file,
            "source_file_sha256": file_sha256,
            "profile_index_in_file": profile_index,
            "source_metadata": json.dumps(source_metadata) if source_metadata is not None else None,
        }

        profile_id_db = self.db_manager.insert_profile(profile_data)

        self.db_manager.add_profile_to_chromadb(
            profile_id_db,
            float_id,
            cycle_number,
            profile_time.to_pydatetime(),
            latitude,
            longitude,
            bgc_params.keys() if bgc_params else [],
            pressure,
            temperature,
            salinity,
        )
        return "inserted"
