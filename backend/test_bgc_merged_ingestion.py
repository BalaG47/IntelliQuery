"""Unified core + BGC profile ingestion tests (Option D).

Synthetic tests exercise the matching rules directly; real-file tests run the
real ingestion path over a handful of files from the local corpus. The dataset
is opened read-only and never modified, and no database is touched: a fake
database manager records the payloads.

Run from ``backend/``::

    python -m unittest test_bgc_merged_ingestion -v
    BGC_DATA_DIR="C:/path/to/bgc raw data" python -m unittest test_bgc_merged_ingestion
"""

import json
import os
import unittest

from bgc_matching import (
    GRID_MISMATCH,
    GRID_MISSING_PRESSURE,
    GRID_OK,
    GRID_PRES_SCALE_SUSPECT,
    classify_pressure_grid,
    logical_identity,
    plan_group,
    product_family,
    profile_time_from_juld,
    select_representative_core,
)
from data_processor import ArgoDataProcessor

# ---------------------------------------------------------------------------
# Synthetic records
# ---------------------------------------------------------------------------

JULD_A = 27040.56319
JULD_D = 27039.15347


def record(
    path="R3902581_001.nc",
    index=0,
    family="core",
    cycle=1,
    juld=JULD_A,
    direction="A",
    data_mode="R",
    levels=100,
    float_id="3902581",
):
    name = os.path.basename(path)
    prefix = "".join(
        character for character in name if character.isalpha())[:2] or "R"
    if prefix not in ("D", "R", "SD", "SR", "BD", "BR"):
        prefix = prefix[:1]
    return {
        "path": path,
        "file": name,
        "prefix": prefix,
        "family": family,
        "index": index,
        "n_prof": 1,
        "float": float_id,
        "cycle": cycle,
        "juld": juld,
        "direction": direction,
        "direction_available": True,
        "data_mode": data_mode,
        "latitude": -1.0,
        "longitude": 80.0,
        "levels": levels,
    }


def core(path="R3902581_001.nc", **kwargs):
    return record(path=path, family="core", **kwargs)


def bgc(path="SR3902581_001.nc", **kwargs):
    return record(path=path, family="bgc", **kwargs)


class ProfileTimeTests(unittest.TestCase):
    def test_juld_becomes_a_whole_second_datetime(self):
        moment = profile_time_from_juld(JULD_A)

        self.assertEqual(moment.microsecond, 0)
        self.assertEqual(moment.isoformat(), "2024-01-13T13:31:00")

    def test_rounding_is_stable(self):
        """The stored identity must be identical on every run (idempotency)."""
        self.assertEqual(profile_time_from_juld(JULD_A),
                         profile_time_from_juld(JULD_A))


class LogicalIdentityTests(unittest.TestCase):
    def test_identity_ignores_the_n_prof_index(self):
        first = core(index=0, path="D3902581_001.nc")
        last = core(index=3, path="D3902581_001.nc", data_mode="D", levels=494)

        self.assertEqual(logical_identity(first), logical_identity(last))

    def test_direction_separates_profiles_of_one_cycle(self):
        ascending = core(index=0, path="D3902581_001.nc", direction="A")
        descending = core(index=0, path="D3902581_001D.nc",
                          direction="D", juld=JULD_D)

        self.assertNotEqual(logical_identity(ascending),
                            logical_identity(descending))
        self.assertEqual(logical_identity(ascending)[2], "A")
        self.assertEqual(logical_identity(descending)[2], "D")

    def test_identity_uses_float_cycle_direction_and_time(self):
        identity = logical_identity(core())

        self.assertEqual(identity[0], 3902581)
        self.assertEqual(identity[1], 1)
        self.assertEqual(identity[2], "A")
        self.assertEqual(identity[3], profile_time_from_juld(JULD_A))

    def test_missing_direction_is_recorded_as_unknown_not_invented(self):
        record_without_direction = core(direction=None)

        self.assertEqual(logical_identity(record_without_direction)[2], "-")


class RepresentativeSelectionTests(unittest.TestCase):
    def test_delayed_variant_at_ninety_percent_is_preferred(self):
        variants = [
            core(index=0, path="D3902581_001.nc", data_mode="D", levels=190),
            core(index=1, path="R3902581_001.nc", data_mode="R", levels=200),
        ]

        chosen, summaries, rule = select_representative_core(variants)

        self.assertIs(chosen, variants[0])
        self.assertEqual(rule, "delayed_mode_at_least_90_percent")
        self.assertEqual(sum(1 for item in summaries if item["selected"]), 1)

    def test_delayed_variant_below_the_threshold_falls_back_to_most_levels(self):
        variants = [
            core(index=0, path="D3902581_001.nc", data_mode="D", levels=170),
            core(index=1, path="R3902581_001.nc", data_mode="R", levels=200),
        ]

        chosen, _, rule = select_representative_core(variants)

        self.assertEqual(chosen["levels"], 200)
        self.assertEqual(chosen["data_mode"], "R")
        self.assertEqual(rule, "most_levels")

    def test_equal_level_counts_prefer_the_delayed_variant(self):
        variants = [
            core(index=0, path="R3902581_001.nc", data_mode="R", levels=100),
            core(index=1, path="D3902581_001.nc", data_mode="D", levels=100),
        ]

        chosen, _, _ = select_representative_core(variants)

        self.assertEqual(chosen["data_mode"], "D")
        # A delayed variant covering 100 % of the maximum is also the first rule.
        self.assertEqual(chosen["levels"], 100)

    def test_every_variant_is_preserved_in_the_provenance(self):
        variants = [
            core(index=0, path="D3902581_001.nc", data_mode="D", levels=243),
            core(index=1, data_mode="R", levels=2),
            core(index=2, data_mode="R", levels=476),
            core(index=3, data_mode="R", levels=494),
        ]

        chosen, summaries, _ = select_representative_core(variants)

        self.assertEqual(chosen["levels"], 494)
        self.assertEqual([item["levels"] for item in summaries],
                         [243, 2, 476, 494])
        self.assertEqual([item["selected"] for item in summaries],
                         [False, False, False, True])
        self.assertEqual(summaries[0]["file"], "D3902581_001.nc")

    def test_no_record_returns_nothing(self):
        self.assertEqual(select_representative_core([]), (None, [], None))


class GroupPlanTests(unittest.TestCase):
    def test_core_and_bgc_of_one_profile_are_merged(self):
        plans = plan_group([core()], [bgc()])

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["match"], "matched")
        self.assertIsNotNone(plans[0]["core"])
        self.assertIsNotNone(plans[0]["bgc"])

    def test_bgc_without_a_core_counterpart_is_its_own_profile(self):
        plans = plan_group([], [bgc()])

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["match"], "bgc_only")
        self.assertIsNone(plans[0]["core"])
        self.assertIsNotNone(plans[0]["bgc"])

    def test_core_without_bgc_stays_a_core_profile(self):
        plans = plan_group([core()], [])

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["match"], "core_only")
        self.assertIsNone(plans[0]["bgc"])

    def test_matching_does_not_use_the_profile_index(self):
        """Same N_PROF index, different observation time: not the same profile."""
        plans = plan_group(
            [core(index=0, juld=JULD_A)],
            [bgc(index=0, juld=JULD_D)],
        )

        self.assertEqual(len(plans), 2)
        self.assertEqual({plan["match"] for plan in plans},
                         {"core_only", "bgc_only"})

    def test_ascending_and_descending_profiles_of_one_cycle_are_kept_apart(self):
        plans = plan_group(
            [core(path="D3902581_001.nc", direction="A", juld=JULD_A),
             core(path="D3902581_001D.nc", direction="D", juld=JULD_D)],
            [bgc(path="SD3902581_001.nc", direction="A", juld=JULD_A),
             bgc(path="SD3902581_001D.nc", direction="D", juld=JULD_D)],
        )

        self.assertEqual(len(plans), 2)
        self.assertTrue(all(plan["match"] == "matched" for plan in plans))
        by_direction = {plan["direction"]: plan for plan in plans}
        self.assertEqual(by_direction["A"]["core"]["file"], "D3902581_001.nc")
        self.assertEqual(by_direction["D"]["core"]["file"], "D3902581_001D.nc")
        self.assertEqual(by_direction["D"]["bgc"]["file"], "SD3902581_001D.nc")

    def test_duplicate_logical_profiles_collapse_into_one_plan(self):
        plans = plan_group(
            [core(index=0, path="D3902581_001.nc", data_mode="D", levels=400),
             core(index=1, path="R3902581_001.nc", data_mode="R", levels=410)],
            [bgc()],
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0]["core_variants"]), 2)
        self.assertEqual(plans[0]["match"], "matched")

    def test_no_records_means_no_plan(self):
        self.assertEqual(plan_group([], []), [])


class PressureGridTests(unittest.TestCase):
    CORE_PRESSURE = [5.0, 10.0, 500.0, 1000.0, 1500.0, 1995.0]

    def test_matching_grid_is_ok(self):
        status, detail = classify_pressure_grid(
            [1.0, 5.0, 100.0, 1000.0, 1500.0, 1996.0], self.CORE_PRESSURE)

        self.assertEqual(status, GRID_OK)
        self.assertEqual(detail["overlap_fraction"], 1.0)

    def test_thousandfold_scale_defect_is_flagged(self):
        status, detail = classify_pressure_grid(
            [0.006, 0.1, 0.5, 1.0, 1.5, 1.996], self.CORE_PRESSURE)

        self.assertEqual(status, GRID_PRES_SCALE_SUSPECT)
        self.assertGreater(detail["median_ratio"], 100)

    def test_missing_pressure(self):
        self.assertEqual(classify_pressure_grid(None)[0], GRID_MISSING_PRESSURE)
        self.assertEqual(
            classify_pressure_grid([None, None])[0], GRID_MISSING_PRESSURE)

    def test_implausible_pressure(self):
        status, detail = classify_pressure_grid([1.0, 2.0, 50000.0])

        self.assertEqual(status, GRID_MISMATCH)
        self.assertIn("plausible", detail["reason"])

    def test_insufficient_overlap(self):
        # Same scale, but the BGC grid and the core grid do not overlap at all.
        status, detail = classify_pressure_grid(
            [50.0, 60.0, 70.0], [100.0, 200.0, 300.0])

        self.assertEqual(status, GRID_MISMATCH)
        self.assertLess(detail["overlap_fraction"], 0.9)

    def test_non_monotonic_grid(self):
        status, detail = classify_pressure_grid(
            [1.0, 500.0, 50.0, 1996.0], self.CORE_PRESSURE)

        self.assertEqual(status, GRID_MISMATCH)
        self.assertIn("monotonic", detail["reason"])

    def test_a_single_level_is_not_a_grid(self):
        self.assertEqual(classify_pressure_grid([1.0])[0], GRID_MISMATCH)

    def test_without_a_core_profile_only_intrinsic_checks_run(self):
        status, detail = classify_pressure_grid([0.006, 0.5, 1.996])

        self.assertEqual(status, GRID_OK)
        self.assertIsNone(detail["median_ratio"])

    def test_values_are_never_rescaled(self):
        values = [0.006, 0.1, 0.5, 1.0, 1.5, 1.996]

        classify_pressure_grid(values, self.CORE_PRESSURE)

        self.assertEqual(values, [0.006, 0.1, 0.5, 1.0, 1.5, 1.996])


class ProductFamilyTests(unittest.TestCase):
    def test_core_and_bgc_prefixes(self):
        self.assertEqual(product_family("D3902581_001.nc"), "core")
        self.assertEqual(product_family("R3902581_001.nc"), "core")
        self.assertEqual(product_family("SD3902581_001.nc"), "bgc")
        self.assertEqual(product_family("SR3902581_001.nc"), "bgc")
        self.assertEqual(product_family("BD3902581_001.nc"), "bgc")
        self.assertEqual(product_family("BR3902581_001.nc"), "bgc")

    def test_uploaded_file_with_another_name_still_ingests(self):
        self.assertEqual(product_family("profile.nc"), "core")


# ---------------------------------------------------------------------------
# Real files
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
BGC_DATA_ROOT = os.environ.get(
    "BGC_DATA_DIR", os.path.join("bgc data", "bgc raw data"))
if not os.path.isabs(BGC_DATA_ROOT):
    BGC_DATA_ROOT = os.path.join(_BACKEND_DIR, BGC_DATA_ROOT)

FLOAT_3902581 = [
    "incois/3902581/profiles/D3902581_001.nc",
    "incois/3902581/profiles/D3902581_001D.nc",
    "incois/3902581/profiles/SD3902581_001.nc",
    "incois/3902581/profiles/SD3902581_001D.nc",
    "incois/3902581/profiles/D3902581_006.nc",
    "incois/3902581/profiles/D3902581_006D.nc",
    "incois/3902581/profiles/SD3902581_006.nc",
    "incois/3902581/profiles/SD3902581_006D.nc",
]
RESCALED_PAIR = [
    "incois/2902306/profiles/D2902306_001.nc",
    "incois/2902306/profiles/SD2902306_001.nc",
]
CONTROLLED_SAMPLE = FLOAT_3902581[:4] + [
    "incois/2902306/profiles/SD2902306_003.nc",
    "incois/2902306/profiles/SD2902306_004.nc",
    "incois/7902190/profiles/SD7902190_019.nc",
    "incois/7902170/profiles/SD7902170_001.nc",
    "incois/2903951/profiles/SR2903951_001.nc",
    "incois/7902200/profiles/SR7902200_015.nc",
    "incois/4903783/profiles/SD4903783_005.nc",
    "incois/6990679/profiles/R6990679_001.nc",
]


def sample_path(relpath):
    return os.path.join(BGC_DATA_ROOT, relpath)


def dataset_available(relpaths):
    return os.path.isdir(BGC_DATA_ROOT) and all(
        os.path.isfile(sample_path(relpath)) for relpath in relpaths)


class FakeDatabaseManager:
    """Records payloads instead of touching MySQL or ChromaDB."""

    def __init__(self):
        self.floats = {}
        self.inserted = []
        self.stored = {}
        self.chroma_writes = []
        self.vector_syncs = []

    def get_or_create_float(self, wmo_number, project_name, platform_type):
        return self.floats.setdefault(wmo_number, len(self.floats) + 1)

    def check_profile_exists(self, float_id, cycle_number, direction=None,
                             profile_time=None):
        return (float_id, cycle_number, direction, profile_time) in self.stored

    def insert_profile(self, profile_data):
        key = (profile_data["float_id"], profile_data["cycle_number"],
               profile_data["direction"], profile_data["profile_time"])
        self.stored[key] = profile_data
        self.inserted.append(profile_data)
        return len(self.stored)

    def get_profile_id(self, float_id, cycle_number, direction=None,
                       profile_time=None):
        key = (float_id, cycle_number, direction, profile_time)
        if key not in self.stored:
            return None
        return self.inserted.index(self.stored[key]) + 1

    def sync_profile_vector(self, profile_id):
        self.vector_syncs.append(profile_id)
        return "unchanged"

    def add_profile_to_chromadb(self, *args, **kwargs):
        self.chroma_writes.append((args, kwargs))

    def add_profile_from_row(self, profile_id):
        # The real manager reads the row back; the fake only records the write.
        self.add_profile_to_chromadb(profile_id)


@unittest.skipUnless(dataset_available(FLOAT_3902581), "dataset not available")
class RealAscendingDescendingTests(unittest.TestCase):
    """Float 3902581 cycles 1 and 6, each with an ascending and a descending
    profile. Opened once per class: each xarray open costs seconds here."""

    @classmethod
    def setUpClass(cls):
        cls.fake_db = FakeDatabaseManager()
        cls.processor = ArgoDataProcessor(cls.fake_db)
        paths = [sample_path(rel) for rel in FLOAT_3902581]
        cls.first = cls.processor.process_paths(paths)
        cls.second = cls.processor.process_paths(paths)

    def profiles(self, cycle=None, direction=None):
        return [
            payload for payload in self.fake_db.inserted
            if (cycle is None or payload["cycle_number"] == cycle)
            and (direction is None or payload["direction"] == direction)
        ]

    def test_one_row_per_logical_profile_per_cycle(self):
        self.assertEqual(self.first["errors"], [])
        self.assertEqual(self.first["inserted"], 4)
        self.assertEqual(self.first["skipped"], 0)
        self.assertEqual(len(self.fake_db.stored), 4)

    def test_ascending_and_descending_profiles_are_both_kept(self):
        by_cycle = {}
        for payload in self.fake_db.inserted:
            by_cycle.setdefault(payload["cycle_number"], set()).add(
                payload["direction"])
        self.assertEqual(by_cycle, {1: {"A", "D"}, 6: {"A", "D"}})

    def test_descending_profile_keeps_its_own_core_and_bgc_source(self):
        descending = self.profiles(cycle=1, direction="D")

        self.assertEqual(len(descending), 1)
        payload = descending[0]
        self.assertEqual(payload["source_file"], "D3902581_001D.nc")
        metadata = json.loads(payload["bgc_source_metadata"])
        self.assertEqual(metadata["source_file"], "SD3902581_001D.nc")
        self.assertEqual(metadata["match"], "matched")
        self.assertEqual(metadata["grid_check"], GRID_OK)

    def test_merged_row_holds_core_and_bgc_measurements(self):
        for payload in self.fake_db.inserted:
            with self.subTest(cycle=payload["cycle_number"],
                              direction=payload["direction"]):
                self.assertIsNotNone(payload["pressure"])
                self.assertIsNotNone(payload["temperature"])
                self.assertIsNotNone(payload["salinity"])
                self.assertIsNotNone(payload["bgc_params"])
                self.assertIsNotNone(payload["bgc_pressure"])
                bgc_params = json.loads(payload["bgc_params"])
                bgc_pressure = json.loads(payload["bgc_pressure"])
                for name, values in bgc_params.items():
                    self.assertEqual(len(values), len(bgc_pressure), name)

    def test_core_variants_are_recorded_in_provenance(self):
        ascending = json.loads(
            self.profiles(cycle=1, direction="A")[0]["source_metadata"])
        descending = json.loads(
            self.profiles(cycle=1, direction="D")[0]["source_metadata"])

        self.assertEqual(len(ascending["core_variants"]), 4)
        self.assertEqual(
            [item["levels"] for item in ascending["core_variants"]],
            [243, 2, 476, 494])
        self.assertEqual(len(descending["core_variants"]), 3)
        self.assertEqual(
            [item["levels"] for item in descending["core_variants"]],
            [224, 1769, 1775])
        self.assertEqual(
            sum(1 for item in ascending["core_variants"] if item["selected"]), 1)

    def test_the_bgc_pressure_axis_is_the_files_own(self):
        for payload in self.fake_db.inserted:
            with self.subTest(cycle=payload["cycle_number"],
                              direction=payload["direction"]):
                metadata = json.loads(payload["bgc_source_metadata"])
                levels = json.loads(payload["bgc_pressure"])
                self.assertEqual(metadata["level_count"], len(levels))
                self.assertAlmostEqual(
                    metadata["grid_check_detail"]["bgc_pressure_max"],
                    max(value for value in levels if value is not None),
                    places=3,
                )

    def test_reingestion_is_idempotent(self):
        self.assertEqual(self.first["inserted"], 4)
        self.assertEqual(self.second["inserted"], 0)
        self.assertEqual(self.second["skipped"], 4)
        self.assertEqual(len(self.fake_db.stored), 4)

    def test_one_chroma_document_per_logical_profile(self):
        self.assertEqual(len(self.fake_db.chroma_writes), 4)


@unittest.skipUnless(dataset_available(RESCALED_PAIR), "dataset not available")
class RealPressureAnomalyTests(unittest.TestCase):
    """SD2902306_001 is one of the 13 files with a rescaled pressure axis."""

    @classmethod
    def setUpClass(cls):
        cls.fake_db = FakeDatabaseManager()
        cls.result = ArgoDataProcessor(cls.fake_db).process_paths(
            [sample_path(rel) for rel in RESCALED_PAIR])

    def test_known_rescaled_pressure_is_detected_and_flagged(self):
        self.assertEqual(self.result["errors"], [])
        self.assertEqual(self.result["inserted"], 1)
        metadata = json.loads(self.fake_db.inserted[0]["bgc_source_metadata"])
        self.assertEqual(metadata["grid_check"], GRID_PRES_SCALE_SUSPECT)
        self.assertGreater(metadata["grid_check_detail"]["median_ratio"], 100)

    def test_the_profile_is_still_stored_with_both_products(self):
        payload = self.fake_db.inserted[0]

        self.assertEqual(metadata_match(payload), "matched")
        # Core pressure comes from the core product, not from the BGC axis.
        core_pressure = [value for value in json.loads(payload["pressure"])
                         if value is not None]
        self.assertGreater(max(core_pressure), 1000.0)

    def test_the_suspect_axis_is_not_rescaled(self):
        payload = self.fake_db.inserted[0]

        bgc_pressure = json.loads(payload["bgc_pressure"])
        self.assertLess(max(value for value in bgc_pressure if value is not None),
                        5.0)
        self.assertEqual(
            len(bgc_pressure),
            len(json.loads(payload["bgc_params"])["PH_IN_SITU_TOTAL"]))

    def test_the_core_only_file_still_ingests_on_its_own(self):
        fake_db = FakeDatabaseManager()
        result = ArgoDataProcessor(fake_db).process_file(
            sample_path(RESCALED_PAIR[0]))

        self.assertIsNone(result["error"])
        self.assertEqual(result["inserted"], 1)
        payload = fake_db.inserted[0]
        self.assertIsNone(payload["bgc_params"])
        self.assertIsNone(payload["bgc_pressure"])
        self.assertIsNone(payload["bgc_source_metadata"])


def metadata_match(payload):
    return json.loads(payload["bgc_source_metadata"])["match"]


@unittest.skipUnless(dataset_available(CONTROLLED_SAMPLE), "dataset not available")
class RealControlledSampleTests(unittest.TestCase):
    """The 12-file sample of the controlled ingestion run."""

    @classmethod
    def setUpClass(cls):
        cls.fake_db = FakeDatabaseManager()
        cls.processor = ArgoDataProcessor(cls.fake_db)
        paths = [sample_path(rel) for rel in CONTROLLED_SAMPLE]
        cls.first = cls.processor.process_paths(paths)
        cls.second = cls.processor.process_paths(paths)

    def test_one_row_per_logical_profile(self):
        """Thirteen N_PROF records (R6990679_001 has four variants) -> 10 rows."""
        self.assertEqual(self.first["errors"], [])
        self.assertEqual(self.first["files"], len(CONTROLLED_SAMPLE))
        self.assertEqual(len(CONTROLLED_SAMPLE), 12)
        self.assertEqual(self.first["inserted"], 10)
        self.assertEqual(len(self.fake_db.stored), 10)

    def test_no_duplicate_logical_identities(self):
        identities = [
            (p["float_id"], p["cycle_number"], p["direction"], p["profile_time"])
            for p in self.fake_db.inserted
        ]

        self.assertEqual(len(identities), len(set(identities)))

    def test_bgc_only_profiles_are_kept_with_their_own_axis(self):
        bgc_only = [p for p in self.fake_db.inserted
                    if p["bgc_source_metadata"]
                    and metadata_match(p) == "bgc_only"]

        self.assertEqual(len(bgc_only), 7)
        for payload in bgc_only:
            self.assertIsNotNone(payload["bgc_pressure"])

    def test_reingestion_is_idempotent(self):
        self.assertEqual(self.second["inserted"], 0)
        self.assertEqual(self.second["skipped"], self.first["inserted"])


if __name__ == "__main__":
    unittest.main()
