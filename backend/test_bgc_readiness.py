"""
BGC-Argo readiness regression tests.

All fixtures in this module are SYNTHETIC. They are constructed in memory with
`xarray.Dataset` to mirror the shape of Argo NetCDF files; they are not real
Argo products and must not be treated as scientific validation of BGC support.

Coverage:
  * parameter-level DATA_MODE extraction independent of BGC measurements
  * full-dataset and already-subset (isel) handling
  * dynamic BGC parameter discovery outside ``known_bgc``
  * synthetic DOXY/CHLA, BBP700/NITRATE, mixed core+BGC, multi-profile cases
  * missing/partial metadata, raw vs adjusted separation, differing lengths
  * unchanged Core-Argo extraction behaviour
  * one integration test with a fake database manager (no MySQL, no ChromaDB)
"""

import json
import os
import unittest

import numpy as np
import xarray as xr

from data_processor import (
    ArgoDataProcessor,
    compute_file_sha256,
    extract_bgc_data,
    extract_data_mode,
    extract_measurement_array,
    extract_profile_qc,
    extract_qc_array,
)

REAL_CORE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "argo_data", "D1900121_044.nc"
)


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------


def build_synthetic(rows, parameters, include_pdm=True, include_station=True,
                    n_levels=3):
    """
    Builds a synthetic multi-profile dataset.

    ``rows`` is a list of dicts with ``mode`` (global DATA_MODE), ``values``
    (parameter -> list of levels), optional ``qc``, ``adjusted``,
    ``adjusted_error``, ``adjusted_qc``, optional ``pdm`` (parameter -> mode),
    and optional ``station`` (list of parameter names for that profile).
    """
    n_prof = len(rows)
    data = {
        "PLATFORM_NUMBER": (("N_PROF",), np.array([b"1900001 "] * n_prof, dtype="S8")),
        "CYCLE_NUMBER": (("N_PROF",), np.array(
            [float(row.get("cycle", index + 1)) for index, row in enumerate(rows)])),
        "JULD": (("N_PROF",), np.array(
            [float(row.get("juld", 19737.5 + index)) for index, row in enumerate(rows)])),
        "LATITUDE": (("N_PROF",), np.array(
            [float(row.get("lat", -10.0 + index)) for index, row in enumerate(rows)])),
        "LONGITUDE": (("N_PROF",), np.array(
            [float(row.get("lon", 48.0 + index)) for index, row in enumerate(rows)])),
        "DATA_MODE": (("N_PROF",), np.array(
            [row["mode"].encode() for row in rows], dtype="S1")),
    }

    station_rows = [row.get("station", parameters) for row in rows]

    if include_station:
        width = max(len(name) for names in station_rows for name in names)
        data["STATION_PARAMETERS"] = (
            ("N_PROF", "N_PARAM"),
            np.array([[name.ljust(width) for name in names] for names in station_rows],
                     dtype=f"S{width}"),
        )

    if include_pdm:
        data["PARAMETER_DATA_MODE"] = (
            ("N_PROF", "N_PARAM"),
            np.array([[row["pdm"][name].encode() for name in names]
                      for row, names in zip(rows, station_rows)], dtype="S1"),
        )

    for parameter in parameters:
        data[parameter] = (
            ("N_PROF", "N_LEVELS"),
            np.array([row["values"][parameter] for row in rows], dtype="f4"),
        )
        if all(parameter in row.get("qc", {}) for row in rows):
            data[f"{parameter}_QC"] = (
                ("N_PROF", "N_LEVELS"),
                np.array([row["qc"][parameter] for row in rows], dtype="S1"),
            )
        if all(parameter in row.get("adjusted", {}) for row in rows):
            data[f"{parameter}_ADJUSTED"] = (
                ("N_PROF", "N_LEVELS"),
                np.array([row["adjusted"][parameter] for row in rows], dtype="f4"),
            )
        if all(parameter in row.get("adjusted_error", {}) for row in rows):
            data[f"{parameter}_ADJUSTED_ERROR"] = (
                ("N_PROF", "N_LEVELS"),
                np.array([row["adjusted_error"][parameter] for row in rows], dtype="f4"),
            )
        if all(parameter in row.get("adjusted_qc", {}) for row in rows):
            data[f"{parameter}_ADJUSTED_QC"] = (
                ("N_PROF", "N_LEVELS"),
                np.array([row["adjusted_qc"][parameter] for row in rows], dtype="S1"),
            )

    return xr.Dataset(data)


def core_row(mode="D", pdm=None, **overrides):
    row = {
        "mode": mode,
        "values": {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
        },
        "qc": {
            "PRES": [b"1", b"1", b"1"],
            "TEMP": [b"1", b"1", b"1"],
            "PSAL": [b"1", b"1", b"1"],
        },
    }
    if pdm is not None:
        row["pdm"] = pdm
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1-2: Core-only datasets
# ---------------------------------------------------------------------------


class CoreOnlyTests(unittest.TestCase):
    """Core datasets keep working, with or without synthetic mode metadata."""

    def test_core_only_without_parameter_data_mode_returns_none(self):
        dataset = build_synthetic(
            [core_row(mode="D")], ["PRES", "TEMP", "PSAL"], include_pdm=False)

        self.assertIsNone(extract_bgc_data(dataset)[-1])
        self.assertIsNone(extract_bgc_data(dataset)[0])

    def test_core_only_with_synthetic_parameter_data_mode_returns_modes(self):
        pdm = {"PRES": "D", "TEMP": "D", "PSAL": "R"}
        dataset = build_synthetic(
            [core_row(mode="D", pdm=pdm)], ["PRES", "TEMP", "PSAL"])

        self.assertEqual(extract_bgc_data(dataset)[-1], pdm)

    def test_core_only_modes_survive_profile_subset(self):
        pdm = {"PRES": "D", "TEMP": "D", "PSAL": "R"}
        dataset = build_synthetic(
            [core_row(mode="D", pdm=pdm)], ["PRES", "TEMP", "PSAL"])

        subset = dataset.isel(N_PROF=0)
        self.assertEqual(extract_bgc_data(subset)[-1], pdm)

    def test_core_only_does_not_derive_modes_from_data_mode(self):
        # Global DATA_MODE is D but no PARAMETER_DATA_MODE exists: no inference.
        dataset = build_synthetic(
            [core_row(mode="D")], ["PRES", "TEMP", "PSAL"], include_pdm=False)

        self.assertIsNone(extract_bgc_data(dataset)[-1])



# ---------------------------------------------------------------------------
# 3-5: BGC measurements
# ---------------------------------------------------------------------------


def bgc_row(parameters, modes, values, mode="D", adjusted=None):
    return {
        "mode": mode,
        "values": values,
        "qc": {name: [b"1"] * len(values[name]) for name in parameters},
        "pdm": modes,
        "adjusted": adjusted or {},
    }


class BgcMeasurementTests(unittest.TestCase):
    def test_doxy_and_chla_are_extracted(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY", "CHLA"]
        modes = {"PRES": "D", "TEMP": "D", "PSAL": "D", "DOXY": "D", "CHLA": "R"}
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "DOXY": [210.0, 205.0, 200.0],
            "CHLA": [0.5, 0.4, 0.3],
        }
        dataset = build_synthetic([bgc_row(parameters, modes, values)], parameters)

        bgc_params, bgc_qc, _, _, _, parameter_data_mode = extract_bgc_data(dataset)

        self.assertEqual(sorted(bgc_params), ["CHLA", "DOXY"])
        self.assertEqual(bgc_params["DOXY"], [210.0, 205.0, 200.0])
        self.assertEqual(bgc_qc["CHLA"], ["1", "1", "1"])
        self.assertEqual(parameter_data_mode, modes)

    def test_bbp700_and_nitrate_are_extracted(self):
        parameters = ["PRES", "TEMP", "PSAL", "BBP700", "NITRATE"]
        modes = {"PRES": "D", "TEMP": "D", "PSAL": "D",
                 "BBP700": "R", "NITRATE": "D"}
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "BBP700": [0.001, 0.002, 0.003],
            "NITRATE": [1.5, 2.5, 3.5],
        }
        dataset = build_synthetic([bgc_row(parameters, modes, values)], parameters)

        bgc_params, _, _, _, _, parameter_data_mode = extract_bgc_data(dataset)

        self.assertEqual(sorted(bgc_params), ["BBP700", "NITRATE"])
        self.assertEqual(parameter_data_mode, modes)

    def test_mixed_core_and_bgc_modes_are_independent_of_data_mode(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY", "CHLA"]
        modes = {"PRES": "D", "TEMP": "R", "PSAL": "D", "DOXY": "R", "CHLA": "D"}
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "DOXY": [210.0, 205.0, 200.0],
            "CHLA": [0.5, 0.4, 0.3],
        }
        dataset = build_synthetic(
            [bgc_row(parameters, modes, values, mode="D")], parameters)

        parameter_data_mode = extract_bgc_data(dataset)[-1]

        self.assertEqual(parameter_data_mode, modes)
        self.assertEqual(extract_data_mode(dataset), "D")
        self.assertNotEqual(parameter_data_mode["TEMP"], "D")
        self.assertNotEqual(parameter_data_mode["DOXY"], "D")


# ---------------------------------------------------------------------------
# 6-7: Single and multi-profile
# ---------------------------------------------------------------------------


class ProfileDimensionTests(unittest.TestCase):
    def test_single_profile_subset_matches_full_dataset(self):
        pdm = {"PRES": "D", "TEMP": "D", "PSAL": "D", "DOXY": "D"}
        parameters = ["PRES", "TEMP", "PSAL", "DOXY"]
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "DOXY": [210.0, 205.0, 200.0],
        }
        dataset = build_synthetic(
            [bgc_row(parameters, pdm, values)], parameters)

        full = extract_bgc_data(dataset)
        subset = extract_bgc_data(dataset.isel(N_PROF=0))

        self.assertEqual(full[-1], subset[-1])
        self.assertEqual(full[0]["DOXY"], subset[0]["DOXY"])

    def test_multi_profile_modes_and_values_are_not_mixed(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY"]

        def row(cycle, mode, pdm, doxy, temp):
            return {
                "cycle": cycle,
                "mode": mode,
                "values": {
                    "PRES": [1.0, 2.0, 3.0],
                    "TEMP": temp,
                    "PSAL": [35.0, 35.1, 35.2],
                    "DOXY": doxy,
                },
                "qc": {name: [b"1", b"1", b"1"] for name in parameters},
                "pdm": pdm,
            }

        dataset = build_synthetic([
            row(902, "D", {"PRES": "D", "TEMP": "D", "PSAL": "D", "DOXY": "D"},
                [210.0, 205.0, 200.0], [28.0, 27.5, 27.0]),
            row(903, "R", {"PRES": "R", "TEMP": "R", "PSAL": "R", "DOXY": "R"},
                [190.0, 185.0, 180.0], [20.0, 19.5, 19.0]),
        ], parameters)

        first = extract_bgc_data(dataset, 0)
        second = extract_bgc_data(dataset, 1)
        first_subset = extract_bgc_data(dataset.isel(N_PROF=0), 0)
        second_subset = extract_bgc_data(dataset.isel(N_PROF=1), 1)

        self.assertEqual(first[0]["DOXY"], [210.0, 205.0, 200.0])
        self.assertEqual(second[0]["DOXY"], [190.0, 185.0, 180.0])
        self.assertEqual(first[-1]["DOXY"], "D")
        self.assertEqual(second[-1]["DOXY"], "R")
        self.assertEqual(first_subset[-1], first[-1])
        self.assertEqual(second_subset[-1], second[-1])
        self.assertEqual(first_subset[0]["DOXY"], first[0]["DOXY"])


# ---------------------------------------------------------------------------
# 8-9: Missing or partial metadata
# ---------------------------------------------------------------------------


class MetadataEdgeCaseTests(unittest.TestCase):
    def test_modes_are_kept_when_bgc_measurements_are_absent(self):
        row = {
            "mode": "D",
            "station": ["PRES", "TEMP", "DOXY"],
            "pdm": {"PRES": "D", "TEMP": "D", "DOXY": "R"},
            "values": {
                "PRES": [1.0, 2.0, 3.0],
                "TEMP": [28.0, 27.5, 27.0],
            },
            "qc": {"PRES": [b"1"] * 3, "TEMP": [b"1"] * 3},
        }
        dataset = build_synthetic([row], ["PRES", "TEMP"])

        bgc_params, _, _, _, _, parameter_data_mode = extract_bgc_data(dataset)

        self.assertIsNone(bgc_params)
        self.assertEqual(parameter_data_mode, {"PRES": "D", "TEMP": "D", "DOXY": "R"})

    def test_modes_without_station_parameters_return_none(self):
        dataset = build_synthetic(
            [core_row(mode="D", pdm={"PRES": "D", "TEMP": "D", "PSAL": "D"})],
            ["PRES", "TEMP", "PSAL"], include_station=False)

        self.assertIsNone(extract_bgc_data(dataset)[-1])

    def test_station_parameters_without_modes_return_none(self):
        dataset = build_synthetic(
            [core_row(mode="D")], ["PRES", "TEMP", "PSAL"], include_pdm=False)

        self.assertIsNone(extract_bgc_data(dataset)[-1])

    def test_mismatched_metadata_lengths_are_not_guessed(self):
        # A scalar PARAMETER_DATA_MODE cannot be aligned with three names, so the
        # metadata must stay unresolved rather than being padded or repeated.
        dataset = xr.Dataset({
            "STATION_PARAMETERS": (("N_PROF", "N_PARAM"),
                                   np.array([[b"PRES", b"TEMP", b"DOXY"]], dtype="S6")),
            "PARAMETER_DATA_MODE": ((), np.array(b"D", dtype="S1")),
            "DOXY": (("N_PROF", "N_LEVELS"), np.array([[1.0, 2.0]], dtype="f4")),
        })

        self.assertIsNone(extract_bgc_data(dataset)[-1])


# ---------------------------------------------------------------------------
# 10-12: Integrity and core regression
# ---------------------------------------------------------------------------


class DataIntegrityTests(unittest.TestCase):
    def test_raw_and_adjusted_measurements_stay_separate(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY", "NITRATE"]
        modes = {"PRES": "D", "TEMP": "D", "PSAL": "D",
                 "DOXY": "D", "NITRATE": "R"}
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "DOXY": [210.0, 205.0, 200.0],
            "NITRATE": [1.5, 2.5, 3.5],
        }
        adjusted = {"DOXY": [211.0, 206.0, 201.0]}
        error = {"DOXY": [0.5, 0.5, 0.5]}
        row = bgc_row(parameters, modes, values, adjusted=adjusted)
        row["adjusted_error"] = error

        dataset = build_synthetic([row], parameters)
        _, _, bgc_adjusted, bgc_adj_error, _, _ = extract_bgc_data(dataset)

        self.assertEqual(bgc_adjusted["DOXY"], [211.0, 206.0, 201.0])
        self.assertEqual(bgc_adj_error["DOXY"], [0.5, 0.5, 0.5])
        # NITRATE has no adjusted arrays in the source: it must stay None, never
        # be filled with the raw values.
        self.assertIsNone(bgc_adjusted["NITRATE"])
        self.assertIsNone(bgc_adj_error["NITRATE"])

    def test_parameters_with_different_native_lengths(self):
        dataset = xr.Dataset({
            "STATION_PARAMETERS": (("N_PROF", "N_PARAM"),
                                   np.array([[b"DOXY", b"NITRATE"]], dtype="S8")),
            "PARAMETER_DATA_MODE": (("N_PROF", "N_PARAM"),
                                    np.array([[b"D", b"R"]], dtype="S1")),
            "DOXY": (("N_PROF", "N_LEVELS"),
                     np.array([[1.0, 2.0, 3.0, 4.0]], dtype="f4")),
            "NITRATE": (("N_PROF", "N_LEVELS_2"),
                        np.array([[5.0, 6.0]], dtype="f4")),
        })

        bgc_params, _, _, _, _, modes = extract_bgc_data(dataset)

        self.assertEqual(len(bgc_params["DOXY"]), 4)
        self.assertEqual(len(bgc_params["NITRATE"]), 2)
        self.assertEqual(modes, {"DOXY": "D", "NITRATE": "R"})

    def test_dynamic_discovery_finds_parameters_outside_known_list(self):
        dataset = xr.Dataset({
            "STATION_PARAMETERS": (("N_PROF", "N_PARAM"),
                                   np.array([[b"PRES", b"TURBIDITY"]], dtype="S10")),
            "PARAMETER_DATA_MODE": (("N_PROF", "N_PARAM"),
                                    np.array([[b"D", b"D"]], dtype="S1")),
            "TURBIDITY": (("N_PROF", "N_LEVELS"),
                          np.array([[0.5, 0.6]], dtype="f4")),
        })

        bgc_params = extract_bgc_data(dataset)[0]
        subset_params = extract_bgc_data(dataset.isel(N_PROF=0))[0]

        self.assertEqual(list(bgc_params), ["TURBIDITY"])
        self.assertEqual(list(subset_params), ["TURBIDITY"])

    def test_core_argo_extraction_is_unchanged(self):
        dataset = build_synthetic(
            [core_row(mode="D")], ["PRES", "TEMP", "PSAL"], include_pdm=False)
        dataset["PROFILE_PRES_QC"] = (("N_PROF",), np.array([b"A"], dtype="S1"))
        profile = dataset.isel(N_PROF=0)

        self.assertEqual(extract_data_mode(profile), "D")
        self.assertEqual(extract_profile_qc(profile, "PROFILE_PRES_QC"), "A")
        self.assertEqual(extract_measurement_array(profile, "PRES"),
                         [1.0, 2.0, 3.0])
        self.assertEqual(extract_qc_array(profile, "PRES_QC"),
                         ["1", "1", "1"])
        self.assertIsNone(extract_bgc_data(profile)[-1])


# ---------------------------------------------------------------------------
# Integration: payload built without a database
# ---------------------------------------------------------------------------


class FakeDatabaseManager:
    """Records payloads instead of touching MySQL or ChromaDB."""

    def __init__(self):
        self.inserted = []
        self.chroma_writes = []
        self.floats = []

    def get_or_create_float(self, wmo_number, project_name, platform_type):
        self.floats.append((wmo_number, project_name, platform_type))
        return 1

    def check_profile_exists(self, float_id, cycle_number):
        return False

    def insert_profile(self, profile_data):
        self.inserted.append(profile_data)
        return len(self.inserted)

    def add_profile_to_chromadb(self, *args, **kwargs):
        self.chroma_writes.append((args, kwargs))


class IngestionPayloadTests(unittest.TestCase):
    def test_parameter_data_mode_reaches_insert_payload(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY"]
        modes = {"PRES": "D", "TEMP": "R", "PSAL": "D", "DOXY": "D"}
        values = {
            "PRES": [1.0, 2.0, 3.0],
            "TEMP": [28.0, 27.5, 27.0],
            "PSAL": [35.0, 35.1, 35.2],
            "DOXY": [210.0, 205.0, 200.0],
        }
        dataset = build_synthetic([bgc_row(parameters, modes, values)], parameters)

        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        status = processor._process_single_profile(
            dataset.isel(N_PROF=0),
            1,
            "1900001",
            nc_file_path="synthetic_bgc_single.nc",
            file_sha256="a" * 64,
            profile_index=0,
            full_dataset=dataset,
        )

        self.assertEqual(status, "inserted")
        self.assertEqual(len(fake_db.inserted), 1)
        self.assertEqual(len(fake_db.chroma_writes), 1)

        payload = fake_db.inserted[0]
        self.assertEqual(json.loads(payload["parameter_data_mode"]), modes)
        self.assertEqual(json.loads(payload["bgc_params"]), {"DOXY": [210.0, 205.0, 200.0]})
        self.assertEqual(json.loads(payload["pressure"]), [1.0, 2.0, 3.0])
        self.assertEqual(payload["data_mode"], "D")
        self.assertEqual(payload["source_file"], "synthetic_bgc_single.nc")
        self.assertEqual(payload["profile_index_in_file"], 0)

    def test_multi_profile_payloads_keep_their_own_modes(self):
        parameters = ["PRES", "TEMP", "PSAL", "DOXY"]

        def row(cycle, mode, pdm, doxy):
            return {
                "cycle": cycle,
                "mode": mode,
                "values": {
                    "PRES": [1.0, 2.0, 3.0],
                    "TEMP": [28.0, 27.5, 27.0],
                    "PSAL": [35.0, 35.1, 35.2],
                    "DOXY": doxy,
                },
                "qc": {name: [b"1"] * 3 for name in parameters},
                "pdm": pdm,
            }

        dataset = build_synthetic([
            row(902, "D", {"PRES": "D", "TEMP": "D", "PSAL": "D", "DOXY": "D"},
                [210.0, 205.0, 200.0]),
            row(903, "R", {"PRES": "R", "TEMP": "R", "PSAL": "R", "DOXY": "R"},
                [190.0, 185.0, 180.0]),
        ], parameters)

        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        for index in range(2):
            processor._process_single_profile(
                dataset.isel(N_PROF=index),
                1,
                "1900001",
                nc_file_path="synthetic_bgc_multi.nc",
                file_sha256="b" * 64,
                profile_index=index,
                full_dataset=dataset,
            )

        first, second = fake_db.inserted
        self.assertEqual(json.loads(first["parameter_data_mode"])["DOXY"], "D")
        self.assertEqual(json.loads(second["parameter_data_mode"])["DOXY"], "R")
        self.assertEqual(json.loads(first["bgc_params"])["DOXY"],
                         [210.0, 205.0, 200.0])
        self.assertEqual(json.loads(second["bgc_params"])["DOXY"],
                         [190.0, 185.0, 180.0])
        self.assertEqual(first["profile_index_in_file"], 0)
        self.assertEqual(second["profile_index_in_file"], 1)

    @unittest.skipUnless(
        os.path.isfile(REAL_CORE_FILE), "real ARGO sample not available")
    def test_real_core_file_ingestion_payload_is_unchanged(self):
        """A real core file still produces the same fields as before the fix."""
        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        result = processor.process_file(REAL_CORE_FILE)

        self.assertIsNone(result["error"])
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["skipped"], 0)

        payload = fake_db.inserted[0]
        self.assertEqual(payload["data_mode"], "D")
        self.assertEqual(payload["profile_pres_qc"], "A")
        self.assertEqual(payload["profile_temp_qc"], "A")
        self.assertEqual(payload["profile_psal_qc"], "A")
        self.assertIsNone(payload["parameter_data_mode"])
        self.assertIsNone(payload["bgc_params"])
        self.assertEqual(len(json.loads(payload["pressure"])), 44)
        self.assertEqual(len(json.loads(payload["temperature_qc"])), 44)
        self.assertEqual(payload["source_file"], "D1900121_044.nc")
        self.assertEqual(payload["source_file_sha256"],
                         compute_file_sha256(REAL_CORE_FILE))


if __name__ == "__main__":
    unittest.main()
