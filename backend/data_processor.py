import json
import logging
import os

import numpy as np
import pandas as pd
import xarray as xr
from tqdm import tqdm

from config import ARGO_DATA_DIR
from database_manager import DatabaseManager


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
            with xr.open_dataset(nc_file_path, decode_times=False) as dataset:
                raw_wmo = dataset["PLATFORM_NUMBER"].values[0]
                wmo_number = int(
                    raw_wmo.decode().strip()
                    if isinstance(raw_wmo, bytes)
                    else str(raw_wmo).strip()
                )
                float_id_db = self.db_manager.get_or_create_float(
                    wmo_number,
                    dataset.attrs.get("project_name", "N/A"),
                    dataset.attrs.get("platform_type", "N/A"),
                )

                for index in range(dataset.sizes["N_PROF"]):
                    status = self._process_single_profile(
                        dataset.isel(N_PROF=index), float_id_db, str(wmo_number)
                    )
                    result[status] += 1

        except Exception as error:
            logging.warning("Could not ingest %s: %s", result["filename"], error)
            result["error"] = str(error)

        return result

    def _process_single_profile(self, profile_ds, float_id_db, float_id):
        """Insert one profile in both stores, or report an existing profile."""

        cycle_number = int(profile_ds["CYCLE_NUMBER"].values)
        if self.db_manager.check_profile_exists(float_id_db, cycle_number):
            return "skipped"

        def get_param(dataset, variable_name):
            if variable_name not in dataset:
                return None
            return [
                float(value) if not np.isnan(value) else None
                for value in dataset[variable_name].values.flatten()
            ]

        profile_time = pd.Timestamp("1950-01-01") + pd.to_timedelta(
            profile_ds["JULD"].values, unit="D"
        )
        pressure = get_param(profile_ds, "PRES")
        temperature = get_param(profile_ds, "TEMP")
        salinity = get_param(profile_ds, "PSAL")
        bgc_params = {
            variable: get_param(profile_ds, variable)
            for variable in ("DOXY", "CHLA", "BBP700", "NITRATE")
            if variable in profile_ds
        }
        latitude = float(profile_ds["LATITUDE"].values)
        longitude = float(profile_ds["LONGITUDE"].values)

        profile_id_db = self.db_manager.insert_profile(
            {
                "float_id": float_id_db,
                "cycle_number": cycle_number,
                "profile_time": profile_time.to_pydatetime(),
                "latitude": latitude,
                "longitude": longitude,
                "pressure": json.dumps(pressure) if pressure is not None else None,
                "temperature": json.dumps(temperature) if temperature is not None else None,
                "salinity": json.dumps(salinity) if salinity is not None else None,
                "bgc_params": json.dumps(bgc_params) if bgc_params else None,
            }
        )
        self.db_manager.add_profile_to_chromadb(
            profile_id_db,
            float_id,
            cycle_number,
            profile_time.to_pydatetime(),
            latitude,
            longitude,
            bgc_params.keys(),
            pressure,
            temperature,
            salinity,
        )
        return "inserted"
