"""
Stage 3B — focused regression tests for profile-level flag extraction.

The regression under test:

``ArgoDataProcessor._process_single_profile`` subsets the dataset with
``isel(N_PROF=i)``, which drops the ``N_PROF`` dimension. Profile-level
character variables (``DATA_MODE``, ``PROFILE_PRES_QC``, ``PROFILE_TEMP_QC``,
``PROFILE_PSAL_QC``) then reach ``extract_data_mode()`` and
``extract_profile_qc()`` as 0-D numpy arrays. The previous ``len(val)`` guard
raised ``TypeError: len() of unsized object`` and aborted ingestion for every
profile.

These tests cover only those two functions. Nothing here touches MySQL,
ChromaDB, sentence-transformers or the wider Phase 2F suite.
"""

import os
import unittest

import numpy as np
import xarray as xr

from data_processor import extract_data_mode, extract_profile_qc

REAL_ARGO_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "argo_data",
    "D1900121_044.nc",
)


def char_var(values):
    """Builds a character variable the way NetCDF/xarray exposes it (object dtype)."""
    return np.array(values, dtype=object)


def profile_dim_dataset(**variables):
    """Builds a dataset with an ``N_PROF`` dimension from per-variable value lists."""
    ds = xr.Dataset()
    for name, values in variables.items():
        ds[name] = (("N_PROF",), char_var(values))
    return ds


class ScalarDataModeTests(unittest.TestCase):
    """``DATA_MODE`` must be readable as a 0-D value."""

    def test_scalar_after_profile_subset_returns_flag(self):
        ds = profile_dim_dataset(DATA_MODE=[b"D"])
        profile = ds.isel(N_PROF=0)

        # Precondition: the subset really is 0-D, i.e. the old len() guard
        # would have raised TypeError here.
        self.assertEqual(np.asarray(profile["DATA_MODE"].values).ndim, 0)

        self.assertEqual(extract_data_mode(profile), "D")

    def test_dimensionless_variable_returns_flag(self):
        ds = xr.Dataset({"DATA_MODE": ((), b"R")})
        self.assertEqual(extract_data_mode(ds), "R")

    def test_scalar_is_uppercased(self):
        ds = profile_dim_dataset(DATA_MODE=[b"a"]).isel(N_PROF=0)
        self.assertEqual(extract_data_mode(ds), "A")

    def test_scalar_blank_returns_none(self):
        ds = profile_dim_dataset(DATA_MODE=[b""]).isel(N_PROF=0)
        self.assertIsNone(extract_data_mode(ds))

    def test_missing_variable_returns_none(self):
        ds = profile_dim_dataset(PROFILE_PRES_QC=[b"A"]).isel(N_PROF=0)
        self.assertIsNone(extract_data_mode(ds))


class ScalarProfileQcTests(unittest.TestCase):
    """``PROFILE_*_QC`` must be readable as a 0-D value."""

    def test_scalar_after_profile_subset_returns_flag(self):
        ds = profile_dim_dataset(PROFILE_PRES_QC=[b"A"]).isel(N_PROF=0)
        self.assertEqual(np.asarray(ds["PROFILE_PRES_QC"].values).ndim, 0)

        self.assertEqual(extract_profile_qc(ds, "PROFILE_PRES_QC"), "A")

    def test_scalar_is_uppercased(self):
        ds = profile_dim_dataset(PROFILE_TEMP_QC=[b"b"]).isel(N_PROF=0)
        self.assertEqual(extract_profile_qc(ds, "PROFILE_TEMP_QC"), "B")

    def test_scalar_fill_values_return_none(self):
        for raw in (b"", b" ", b"\x00"):
            with self.subTest(raw=raw):
                ds = profile_dim_dataset(PROFILE_PRES_QC=[raw]).isel(N_PROF=0)
                self.assertIsNone(extract_profile_qc(ds, "PROFILE_PRES_QC"))

    def test_scalar_outside_admt_domain_returns_none(self):
        for raw in (b"X", b"0", b"9"):
            with self.subTest(raw=raw):
                ds = profile_dim_dataset(PROFILE_PSAL_QC=[raw]).isel(N_PROF=0)
                self.assertIsNone(
                    extract_profile_qc(ds, "PROFILE_PSAL_QC")
                )

    def test_missing_variable_returns_none(self):
        ds = profile_dim_dataset(DATA_MODE=[b"D"]).isel(N_PROF=0)
        self.assertIsNone(extract_profile_qc(ds, "PROFILE_PRES_QC"))


class ArrayBehaviourIsPreservedTests(unittest.TestCase):
    """Multi-profile datasets must still index the requested profile."""

    def test_data_mode_indexes_requested_profile(self):
        ds = profile_dim_dataset(DATA_MODE=[b"D", b"R", b"A"])
        self.assertEqual(
            [extract_data_mode(ds, i) for i in range(3)],
            ["D", "R", "A"],
        )

    def test_profile_qc_indexes_requested_profile(self):
        ds = profile_dim_dataset(PROFILE_PRES_QC=[b"A", b"F", b"B"])
        self.assertEqual(
            [
                extract_profile_qc(ds, "PROFILE_PRES_QC", i)
                for i in range(3)
            ],
            ["A", "F", "B"],
        )

    def test_valid_indices_still_extract_scalars_and_arrays(self):
        ds = profile_dim_dataset(
            DATA_MODE=[b"D", b"R"],
            PROFILE_PRES_QC=[b"C", b"F"],
        )
        self.assertEqual(extract_data_mode(ds, 0), "D")
        self.assertEqual(extract_data_mode(ds, 1), "R")
        self.assertEqual(extract_profile_qc(ds, "PROFILE_PRES_QC", 0), "C")
        self.assertEqual(extract_profile_qc(ds, "PROFILE_PRES_QC", 1), "F")

        scalar = ds.isel(N_PROF=1)
        self.assertEqual(extract_data_mode(scalar), "R")
        self.assertEqual(extract_profile_qc(scalar, "PROFILE_PRES_QC"), "F")


class InvalidIndexReturnsNoneTests(unittest.TestCase):
    """A missing profile must never borrow another profile's value."""

    def setUp(self):
        self.ds = profile_dim_dataset(
            DATA_MODE=[b"D", b"R"],
            PROFILE_PRES_QC=[b"C", b"F"],
        )

    def test_data_mode_out_of_range_positive_index_returns_none(self):
        self.assertIsNone(extract_data_mode(self.ds, 2))
        self.assertIsNone(extract_data_mode(self.ds, 99))

    def test_data_mode_negative_index_returns_none(self):
        self.assertIsNone(extract_data_mode(self.ds, -1))
        self.assertIsNone(extract_data_mode(self.ds, -99))

    def test_profile_qc_out_of_range_positive_index_returns_none(self):
        self.assertIsNone(
            extract_profile_qc(self.ds, "PROFILE_PRES_QC", 2)
        )
        self.assertIsNone(
            extract_profile_qc(self.ds, "PROFILE_PRES_QC", 99)
        )

    def test_profile_qc_negative_index_returns_none(self):
        self.assertIsNone(
            extract_profile_qc(self.ds, "PROFILE_PRES_QC", -1)
        )
        self.assertIsNone(
            extract_profile_qc(self.ds, "PROFILE_PRES_QC", -99)
        )


class EmptyAndSingleElementArrayTests(unittest.TestCase):
    """One-dimensional arrays that are empty or hold exactly one profile."""

    def test_data_mode_empty_array_returns_none(self):
        ds = profile_dim_dataset(DATA_MODE=[])
        values = np.asarray(ds["DATA_MODE"].values)

        self.assertEqual(ds.sizes["N_PROF"], 0)
        self.assertEqual(values.ndim, 1)
        self.assertEqual(values.size, 0)

        self.assertIsNone(extract_data_mode(ds))

    def test_profile_qc_empty_array_returns_none(self):
        ds = profile_dim_dataset(PROFILE_PRES_QC=[])
        values = np.asarray(ds["PROFILE_PRES_QC"].values)

        self.assertEqual(ds.sizes["N_PROF"], 0)
        self.assertEqual(values.ndim, 1)
        self.assertEqual(values.size, 0)

        self.assertIsNone(extract_profile_qc(ds, "PROFILE_PRES_QC"))

    def test_data_mode_single_element_array_is_extracted(self):
        ds = profile_dim_dataset(DATA_MODE=[b"R"])
        values = np.asarray(ds["DATA_MODE"].values)

        # A 1-D array of length one, not a 0-D scalar: no slicing happens.
        self.assertEqual(values.ndim, 1)
        self.assertEqual(values.size, 1)

        self.assertEqual(extract_data_mode(ds), "R")

    def test_profile_qc_single_element_array_is_extracted(self):
        ds = profile_dim_dataset(PROFILE_PSAL_QC=[b"B"])
        values = np.asarray(ds["PROFILE_PSAL_QC"].values)

        self.assertEqual(values.ndim, 1)
        self.assertEqual(values.size, 1)

        self.assertEqual(extract_profile_qc(ds, "PROFILE_PSAL_QC"), "B")


@unittest.skipUnless(
    os.path.isfile(REAL_ARGO_FILE),
    "real ARGO NetCDF sample not available",
)
class RealArgoFileTests(unittest.TestCase):
    """End-to-end check against a real ARGO file, the shape ingestion sees."""

    def test_every_profile_is_readable(self):
        extracted = 0

        with xr.open_dataset(REAL_ARGO_FILE, decode_times=False) as dataset:
            self.assertIn("N_PROF", dataset.sizes)

            for index in range(dataset.sizes["N_PROF"]):
                profile = dataset.isel(N_PROF=index)

                data_mode = extract_data_mode(profile)
                self.assertIn(data_mode, {"D", "R", "A", None})

                for var_name in (
                    "PROFILE_PRES_QC",
                    "PROFILE_TEMP_QC",
                    "PROFILE_PSAL_QC",
                ):
                    if var_name not in profile.variables:
                        continue
                    self.assertIn(
                        extract_profile_qc(profile, var_name),
                        {"A", "B", "C", "D", "E", "F", None},
                    )

                if data_mode is not None:
                    extracted += 1

        self.assertGreater(
            extracted,
            0,
            "expected at least one profile to yield a DATA_MODE flag",
        )


if __name__ == "__main__":
    unittest.main()
