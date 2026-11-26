"""Real-file BGC-Argo extraction validation.

Unlike ``test_bgc_readiness`` (synthetic fixtures), every test here opens a real
NetCDF product from the locally downloaded Indian Ocean BGC-Argo subset and
compares what ``data_processor`` extracts against the file itself.

The dataset is not committed. Point ``BGC_DATA_DIR`` at the extracted corpus, or
leave it unset to use ``backend/bgc data/bgc raw data``. When the directory is
absent the whole module skips, so a checkout without the data still runs green.

The dataset is opened read-only and never modified. Only ten files are used, so
the module stays proportionate even though the corpus holds 1,264 files.

Run from ``backend/``::

    python -m unittest test_bgc_real_files -v
    BGC_DATA_DIR="C:/path/to/bgc raw data" python -m unittest test_bgc_real_files

Each file is opened once and kept in memory for the whole module: in this
environment ``xarray.open_dataset`` costs several seconds per call while
``netCDF4`` opens the same file in milliseconds, and the tests re-read ten files.
"""

import functools
import json
import os
import unittest

import numpy as np
import xarray as xr

from data_processor import (
    ArgoDataProcessor,
    extract_bgc_data,
    extract_data_mode,
    extract_measurement_array,
)

# ---------------------------------------------------------------
# Sample definition
# ---------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
BGC_DATA_ROOT = os.environ.get(
    "BGC_DATA_DIR", os.path.join("bgc data", "bgc raw data")
)
if not os.path.isabs(BGC_DATA_ROOT):
    BGC_DATA_ROOT = os.path.join(_BACKEND_DIR, BGC_DATA_ROOT)

# Ten files chosen to span floats, prefixes (SD/SR/D/R), parameter sets,
# measurement lengths, parameter data modes, and one file with no position fix.
#
#   relpath                                        why it is in the sample
#   ---------------------------------------------  -----------------------
#   SD2902306_003  all five target BGC parameters, modes D/R/A, 1033 levels
#   SD7902190_019  all five targets on a second float, 8 station params
#   SD7902170_001  pH but no NITRATE, 7 station params
#   SR2903951_001  real-time synthetic, shortest profile (174 levels)
#   SR7902200_015  reduced parameter set (no BBP700 / pH / NITRATE)
#   SD3902581_001  synthetic delayed, 735 levels, CHLA_FLUORESCENCE
#   SD3902581_001D "D"-suffixed variant, no valid position fix
#   D3902581_001   core-only, 4 sub-profiles sharing one cycle
#   D2902306_001   core-only, single profile, 996 levels
#   R6990679_001   core real-time, 4 sub-profiles sharing one cycle
SAMPLE_FILES = [
    ("incois/2902306/profiles/SD2902306_003.nc", "all-five-targets"),
    ("incois/7902190/profiles/SD7902190_019.nc", "all-five-targets-second-float"),
    ("incois/7902170/profiles/SD7902170_001.nc", "ph-without-nitrate"),
    ("incois/2903951/profiles/SR2903951_001.nc", "shortest-profile"),
    ("incois/7902200/profiles/SR7902200_015.nc", "reduced-parameter-set"),
    ("incois/3902581/profiles/SD3902581_001.nc", "synthetic-delayed-mixed-modes"),
    ("incois/3902581/profiles/SD3902581_001D.nc", "no-position-fix"),
    ("incois/3902581/profiles/D3902581_001.nc", "core-only-multi-profile"),
    ("incois/2902306/profiles/D2902306_001.nc", "core-only-single-profile"),
    ("incois/6990679/profiles/R6990679_001.nc", "core-realtime-multi-profile"),
]

TARGET_BGC = ("DOXY", "CHLA", "NITRATE", "BBP700", "PH_IN_SITU_TOTAL")
CORE = ("PRES", "TEMP", "PSAL")
CORE_DERIVED = (
    "MTIME", "TEMP_STD", "PSAL_STD", "PRES_MED", "TEMP_MED", "PSAL_MED",
)

# Fill values ``extract_measurement_array`` treats as missing.
FILL_VALUES = (99999.0, 60000.0, -99999.0)

DATASET_AVAILABLE = os.path.isdir(BGC_DATA_ROOT) and all(
    os.path.isfile(os.path.join(BGC_DATA_ROOT, rel)) for rel, _ in SAMPLE_FILES
)

SKIP_REASON = (
    f"real BGC dataset not available at {BGC_DATA_ROOT} "
    "(set BGC_DATA_DIR to the extracted corpus)"
)


def sample_path(relpath):
    return os.path.join(BGC_DATA_ROOT, relpath)


@functools.lru_cache(maxsize=None)
def load_dataset(relpath):
    """Open one sample file once, load it fully, and detach it from the handle."""
    dataset = xr.open_dataset(sample_path(relpath), decode_times=False)
    loaded = dataset.load()
    dataset.close()
    return loaded


# ---------------------------------------------------------------
# Source-side helpers (independent of data_processor)
# ---------------------------------------------------------------


def source_row(profile, var):
    """Return the 1-D source values of ``var`` for an isel'ed profile."""
    return np.asarray(profile[var].values).ravel()


def source_expected(profile, var):
    """Source values normalised the way a missing value should appear."""
    expected = []
    for value in source_row(profile, var):
        if isinstance(value, (bytes, np.bytes_)):
            text = value.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
            expected.append(text or None)
            continue
        if isinstance(value, (str, np.str_)):
            text = str(value).strip("\x00 \t\r\n")
            expected.append(text or None)
            continue
        number = float(value)
        if np.isnan(number) or number in FILL_VALUES:
            expected.append(None)
        else:
            expected.append(round(number, 4))
    return expected


def station_parameters(profile):
    if "STATION_PARAMETERS" not in profile.variables:
        return []
    names = []
    for item in np.atleast_1d(profile["STATION_PARAMETERS"].values):
        text = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
        text = text.strip("\x00 \t\r\n")
        if text:
            names.append(text.upper())
    return names


class FakeDatabaseManager:
    """Records payloads instead of touching MySQL or ChromaDB."""

    def __init__(self):
        self.floats = {}
        self.inserted = []
        self.chroma_writes = []

    def get_or_create_float(self, wmo_number, project_name, platform_type):
        return self.floats.setdefault(wmo_number, len(self.floats) + 1)

    def check_profile_exists(self, float_id, cycle_number, direction=None,
                             profile_time=None):
        return False

    def insert_profile(self, profile_data):
        self.inserted.append(profile_data)
        return len(self.inserted)

    def get_profile_id(self, float_id, cycle_number, direction=None,
                       profile_time=None):
        return None

    def sync_profile_vector(self, profile_id):
        raise AssertionError("no stored profile can need reconciliation")

    def add_profile_to_chromadb(self, *args, **kwargs):
        self.chroma_writes.append((args, kwargs))

    def add_profile_from_row(self, profile_id):
        # The real manager reads the row back; the fake only records the write.
        self.add_profile_to_chromadb(profile_id)


class IdentityKeyDatabaseManager(FakeDatabaseManager):
    """Fake manager enforcing UNIQUE (float, cycle, direction, profile_time)."""

    def __init__(self):
        super().__init__()
        self.stored = {}

    @staticmethod
    def key(profile_data):
        return (profile_data["float_id"], profile_data["cycle_number"],
                profile_data["direction"], profile_data["profile_time"])

    def check_profile_exists(self, float_id, cycle_number, direction=None,
                             profile_time=None):
        return (float_id, cycle_number, direction, profile_time) in self.stored

    def get_profile_id(self, float_id, cycle_number, direction=None,
                       profile_time=None):
        key = (float_id, cycle_number, direction, profile_time)
        if key not in self.stored:
            return None
        return self.inserted.index(self.stored[key]) + 1

    def sync_profile_vector(self, profile_id):
        return "unchanged"

    def insert_profile(self, profile_data):
        self.stored[self.key(profile_data)] = profile_data
        self.inserted.append(profile_data)
        return len(self.stored)


# ---------------------------------------------------------------
# 1. Dataset inventory invariants
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class DatasetInventoryTests(unittest.TestCase):
    def test_each_sample_file_is_a_readable_netcdf(self):
        for relpath, _ in SAMPLE_FILES:
            with self.subTest(file=relpath):
                dataset = load_dataset(relpath)
                self.assertGreaterEqual(dataset.sizes["N_PROF"], 1)
                self.assertGreaterEqual(dataset.sizes["N_LEVELS"], 1)
                self.assertIn("PLATFORM_NUMBER", dataset.variables)
                self.assertIn("CYCLE_NUMBER", dataset.variables)

    def test_five_target_parameters_are_present_in_the_dataset(self):
        present = set()
        for relpath, _ in SAMPLE_FILES:
            dataset = load_dataset(relpath)
            present.update(name for name in TARGET_BGC if name in dataset.variables)
        self.assertEqual(present, set(TARGET_BGC))

    def test_core_parameters_are_present_in_every_sample_file(self):
        for relpath, _ in SAMPLE_FILES:
            with self.subTest(file=relpath):
                dataset = load_dataset(relpath)
                for name in CORE:
                    self.assertIn(name, dataset.variables)


# ---------------------------------------------------------------
# 2. Parameter extraction matches the source file
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class ParameterExtractionTests(unittest.TestCase):
    def test_every_station_parameter_is_extracted_or_explicitly_core(self):
        """No available parameter may be silently dropped."""
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                bgc_params = extract_bgc_data(
                    profile, index, metadata_dataset=full
                )[0] or {}
                expected = [
                    name for name in station_parameters(profile)
                    if name not in CORE
                    and name not in CORE_DERIVED
                    and name in profile.variables
                ]
                with self.subTest(file=relpath, profile=index):
                    self.assertEqual(sorted(bgc_params), sorted(expected))

    def test_raw_measurements_match_source_values(self):
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                bgc_params = extract_bgc_data(
                    profile, index, metadata_dataset=full
                )[0] or {}
                for name, values in bgc_params.items():
                    with self.subTest(file=relpath, profile=index, param=name):
                        self.assertEqual(values, source_expected(profile, name))

    def test_all_five_target_parameters_are_extracted_from_one_profile(self):
        full = load_dataset("incois/2902306/profiles/SD2902306_003.nc")
        profile = full.isel(N_PROF=0)
        bgc_params, bgc_qc, bgc_adj, bgc_err, bgc_adj_qc, modes = \
            extract_bgc_data(profile, 0, metadata_dataset=full)

        for name in TARGET_BGC:
            with self.subTest(param=name):
                self.assertIn(name, bgc_params)
                self.assertEqual(len(bgc_params[name]), full.sizes["N_LEVELS"])
                self.assertGreater(
                    sum(1 for v in bgc_params[name] if v is not None), 0)
                self.assertIsNotNone(bgc_qc[name])
                self.assertIsNotNone(bgc_adj[name])
                self.assertIsNotNone(bgc_err[name])
                self.assertIsNotNone(bgc_adj_qc[name])
                self.assertEqual(len(bgc_qc[name]), len(bgc_params[name]))

        self.assertEqual(modes["NITRATE"], "R")
        self.assertEqual(modes["PH_IN_SITU_TOTAL"], "R")
        self.assertEqual(modes["DOXY"], "A")
        self.assertEqual(modes["PRES"], "D")

    def test_dynamic_discovery_finds_chla_fluorescence(self):
        """CHLA_FLUORESCENCE is outside the predefined list; it must still appear."""
        full = load_dataset("incois/2903951/profiles/SR2903951_001.nc")
        bgc_params = extract_bgc_data(full.isel(N_PROF=0), 0,
                                      metadata_dataset=full)[0]
        self.assertIn("CHLA_FLUORESCENCE", bgc_params)
        self.assertNotIn("CHLA_FLUORESCENCE_dPRES", bgc_params)

    def test_parameters_absent_from_a_file_are_not_reported(self):
        """A file with no pH/NITRATE must not invent them."""
        full = load_dataset("incois/2903951/profiles/SR2903951_001.nc")
        bgc_params = extract_bgc_data(full.isel(N_PROF=0), 0,
                                      metadata_dataset=full)[0]
        self.assertNotIn("NITRATE", bgc_params)
        self.assertNotIn("PH_IN_SITU_TOTAL", bgc_params)


# ---------------------------------------------------------------
# 3. Array alignment: QC, adjusted values, errors, modes
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class ArrayAlignmentTests(unittest.TestCase):
    def test_qc_and_adjusted_arrays_align_with_their_measurements(self):
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                params, qc, adj, err, adj_qc = extract_bgc_data(
                    profile, index, metadata_dataset=full)[:5]
                for name, values in (params or {}).items():
                    for bundle, suffix in (
                        (qc, f"{name}_QC"),
                        (adj, f"{name}_ADJUSTED"),
                        (err, f"{name}_ADJUSTED_ERROR"),
                        (adj_qc, f"{name}_ADJUSTED_QC"),
                    ):
                        with self.subTest(file=relpath, profile=index,
                                          param=name, field=suffix):
                            self.assertEqual(suffix in profile.variables,
                                             bundle[name] is not None)
                            if suffix in profile.variables:
                                self.assertEqual(len(bundle[name]), len(values))

    def test_qc_flags_match_the_source_qc_values(self):
        full = load_dataset("incois/2902306/profiles/SD2902306_003.nc")
        profile = full.isel(N_PROF=0)
        bgc_qc = extract_bgc_data(profile, 0, metadata_dataset=full)[1]
        for name in TARGET_BGC:
            with self.subTest(param=name):
                expected = source_expected(profile, f"{name}_QC")
                self.assertEqual(bgc_qc[name], expected)
                self.assertEqual(len(bgc_qc[name]), len(source_row(profile, name)))

    def test_adjusted_values_match_the_source_adjusted_variable(self):
        """The extractor must read ``<PARAM>_ADJUSTED``, never copy the raw array."""
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                params, _, adj, err, adj_qc = extract_bgc_data(
                    profile, index, metadata_dataset=full)[:5]
                for name in (params or {}):
                    for bundle, suffix in (
                        (adj, f"{name}_ADJUSTED"),
                        (err, f"{name}_ADJUSTED_ERROR"),
                        (adj_qc, f"{name}_ADJUSTED_QC"),
                    ):
                        if suffix not in profile.variables:
                            continue
                        with self.subTest(file=relpath, profile=index,
                                          param=name, field=suffix):
                            self.assertEqual(bundle[name],
                                             source_expected(profile, suffix))

    def test_parameter_data_modes_match_the_source_metadata(self):
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                modes = extract_bgc_data(profile, index,
                                         metadata_dataset=full)[5]
                if "PARAMETER_DATA_MODE" not in full.variables:
                    with self.subTest(file=relpath, profile=index):
                        self.assertIsNone(modes)
                    continue
                names = station_parameters(profile)
                raw = np.atleast_2d(full["PARAMETER_DATA_MODE"].values)[index]
                expected = {}
                for name, flag in zip(names, np.atleast_1d(raw)):
                    flag = flag.decode(errors="replace").strip() \
                        if isinstance(flag, bytes) else str(flag).strip()
                    if name and flag:
                        expected[name] = flag.upper()
                with self.subTest(file=relpath, profile=index):
                    self.assertEqual(modes, expected)


# ---------------------------------------------------------------
# 4. Missing-value and metadata handling
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class MissingValueTests(unittest.TestCase):
    def test_fill_values_become_null_and_lengths_are_preserved(self):
        full = load_dataset("incois/2902306/profiles/SD2902306_003.nc")
        profile = full.isel(N_PROF=0)
        params = extract_bgc_data(profile, 0, metadata_dataset=full)[0]
        for name, values in params.items():
            with self.subTest(param=name):
                self.assertEqual(len(values), len(source_row(profile, name)))
                self.assertNotIn(99999.0, values)
                self.assertNotIn(60000.0, values)
                self.assertFalse(any(
                    isinstance(value, float) and np.isnan(value)
                    for value in values
                ))

    def test_sparse_parameter_keeps_its_levels_but_marks_missing_ones(self):
        """DOXY is measured on 75 of 1,036 levels; missing levels stay null."""
        full = load_dataset("incois/7902190/profiles/SD7902190_019.nc")
        profile = full.isel(N_PROF=0)
        doxy = extract_bgc_data(profile, 0, metadata_dataset=full)[0]["DOXY"]
        present = sum(1 for value in doxy if value is not None)
        self.assertEqual(len(doxy), full.sizes["N_LEVELS"])
        self.assertGreater(present, 0)
        self.assertLess(present, len(doxy))
        self.assertIn(None, doxy)

    def test_profile_without_position_still_extracts_bgc(self):
        full = load_dataset("incois/3902581/profiles/SD3902581_001D.nc")
        profile = full.isel(N_PROF=0)
        params, _, _, _, _, modes = extract_bgc_data(profile, 0,
                                                      metadata_dataset=full)
        self.assertTrue(np.isnan(float(profile["LATITUDE"].values)))
        self.assertIn("DOXY", params)
        self.assertEqual(len(params["DOXY"]), full.sizes["N_LEVELS"])
        self.assertIsNotNone(modes)

    def test_files_without_global_data_mode_report_no_mode(self):
        """Synthetic BGC files carry PARAMETER_DATA_MODE but no DATA_MODE."""
        full = load_dataset("incois/2902306/profiles/SD2902306_003.nc")
        self.assertNotIn("DATA_MODE", full.variables)
        self.assertIsNone(extract_data_mode(full.isel(N_PROF=0)))

    def test_core_files_do_report_a_global_data_mode(self):
        full = load_dataset("incois/2902306/profiles/D2902306_001.nc")
        self.assertEqual(extract_data_mode(full.isel(N_PROF=0)), "D")


# ---------------------------------------------------------------
# 5. Core parameter regression
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class CoreRegressionTests(unittest.TestCase):
    def test_core_measurements_match_source_before_and_after_bgc_extraction(self):
        for relpath, _ in SAMPLE_FILES:
            full = load_dataset(relpath)
            for index in range(full.sizes["N_PROF"]):
                profile = full.isel(N_PROF=index)
                before = {name: extract_measurement_array(profile, name)
                          for name in CORE}
                extract_bgc_data(profile, index, metadata_dataset=full)
                after = {name: extract_measurement_array(profile, name)
                         for name in CORE}
                with self.subTest(file=relpath, profile=index):
                    for name in CORE:
                        self.assertEqual(before[name], after[name])
                        if name in profile.variables:
                            self.assertEqual(after[name],
                                             source_expected(profile, name))

    def test_core_auxiliary_parameters_are_excluded_from_bgc(self):
        """MTIME/*_STD/*_MED are core-derived, not BGC measurements (B3)."""
        full = load_dataset("incois/3902581/profiles/D3902581_001.nc")
        profile = full.isel(N_PROF=0)
        params = extract_bgc_data(profile, 0, metadata_dataset=full)[0] or {}

        for name in CORE_DERIVED:
            self.assertIn(name, station_parameters(profile))
            self.assertNotIn(name, params)
        for name in CORE:
            self.assertNotIn(name, params)
        # That core-only file has nothing left after the exclusion.
        self.assertEqual(params, {})


# ---------------------------------------------------------------
# 6. Ingestion payload / database representation
# ---------------------------------------------------------------


@unittest.skipUnless(DATASET_AVAILABLE, SKIP_REASON)
class IngestionPayloadTests(unittest.TestCase):
    def test_real_bgc_file_builds_a_serialisable_payload(self):
        relpath = "incois/2902306/profiles/SD2902306_003.nc"
        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        result = processor.process_file(sample_path(relpath))

        self.assertIsNone(result["error"])
        self.assertEqual(result["inserted"], 1)
        payload = fake_db.inserted[0]

        params = json.loads(payload["bgc_params"])
        self.assertEqual(set(params), set(TARGET_BGC))

        for field in ("bgc_qc", "bgc_adjusted", "bgc_adjusted_error",
                      "bgc_adjusted_qc", "parameter_data_mode"):
            self.assertIsNotNone(payload[field], field)
            decoded = json.loads(payload[field])
            self.assertIsInstance(decoded, dict, field)
            self.assertNotIn("NaN", payload[field], field)

        self.assertEqual(json.loads(payload["parameter_data_mode"])["NITRATE"], "R")
        self.assertEqual(len(json.loads(payload["pressure"])),
                         len(params["DOXY"]))
        self.assertEqual(len(json.loads(payload["pressure_qc"])),
                         len(params["DOXY"]))
        self.assertEqual(payload["source_file"], "SD2902306_003.nc")
        self.assertEqual(len(payload["source_file_sha256"]), 64)
        self.assertEqual(payload["profile_index_in_file"], 0)
        self.assertEqual(len(fake_db.chroma_writes), 1)
        # The BGC array keeps the axis it was delivered with.
        self.assertIn(payload["direction"], ("A", "D"))
        self.assertEqual(json.loads(payload["bgc_pressure"]),
                         json.loads(payload["pressure"]))
        metadata = json.loads(payload["bgc_source_metadata"])
        self.assertEqual(metadata["product_type"], "synthetic_delayed")
        self.assertEqual(metadata["grid_check"], "ok")
        # No core product was given, so the BGC profile is stored on its own
        # rather than matched to something that was not supplied.
        self.assertEqual(metadata["match"], "bgc_only")

    def test_multi_profile_file_becomes_one_logical_profile(self):
        """Four N_PROF entries are four variants of one physical profile."""
        relpath = "incois/3902581/profiles/D3902581_001.nc"
        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        result = processor.process_file(sample_path(relpath))

        self.assertIsNone(result["error"])
        self.assertEqual(result["inserted"], 1)
        payload = fake_db.inserted[0]
        self.assertEqual(payload["direction"], "A")
        variants = json.loads(payload["source_metadata"])["core_variants"]
        self.assertEqual([v["profile_index_in_file"] for v in variants],
                         [0, 1, 2, 3])
        # No delayed variant keeps 90 % of the maximum level count here, so the
        # rule falls back to the variant with the most levels.
        self.assertEqual([v["levels"] for v in variants], [243, 2, 476, 494])
        self.assertEqual([v["selected"] for v in variants],
                         [False, False, False, True])
        self.assertEqual(payload["profile_index_in_file"], 3)
        self.assertEqual(json.loads(payload["pressure"]),
                         extract_measurement_array(
                             load_dataset(relpath).isel(N_PROF=3), "PRES"))

    def test_multi_profile_file_is_stored_once(self):
        """One row per logical profile; the variants survive as provenance."""
        relpath = "incois/3902581/profiles/D3902581_001.nc"
        full = load_dataset(relpath)
        cycles = set(np.atleast_1d(full["CYCLE_NUMBER"].values).tolist())

        fake_db = IdentityKeyDatabaseManager()
        processor = ArgoDataProcessor(fake_db)
        result = processor.process_file(sample_path(relpath))

        self.assertIsNone(result["error"])
        self.assertEqual(len(cycles), 1)
        self.assertEqual(full.sizes["N_PROF"], 4)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(fake_db.stored), 1)
        key = next(iter(fake_db.stored))
        self.assertEqual(key[1], 1)
        self.assertEqual(key[2], "A")

    def test_reingesting_the_same_file_is_idempotent(self):
        relpath = "incois/3902581/profiles/D3902581_001.nc"
        fake_db = IdentityKeyDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        first = processor.process_file(sample_path(relpath))
        second = processor.process_file(sample_path(relpath))

        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(fake_db.stored), 1)

    def test_payload_records_a_missing_position_fix_as_null(self):
        """No fix means SQL NULL; a NaN must never reach the database (B1)."""
        relpath = "incois/3902581/profiles/SD3902581_001D.nc"
        fake_db = FakeDatabaseManager()
        processor = ArgoDataProcessor(fake_db)

        result = processor.process_file(sample_path(relpath))

        self.assertIsNone(result["error"])
        self.assertEqual(result["inserted"], 1)
        payload = fake_db.inserted[0]
        self.assertIsNone(payload["latitude"])
        self.assertIsNone(payload["longitude"])
        # The measurements of a profile without a fix are still stored.
        self.assertIsNotNone(payload["pressure"])
        self.assertIsNotNone(payload["bgc_params"])
        self.assertIsNotNone(payload["bgc_adjusted"])


if __name__ == "__main__":
    unittest.main()
