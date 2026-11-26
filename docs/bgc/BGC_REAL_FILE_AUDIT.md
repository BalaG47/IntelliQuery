# IntelliQuery — Real BGC-Argo Dataset Validation & Extraction Audit

**Scope:** inspection and validation of the existing BGC extraction implementation
(`backend/data_processor.py`) against the real Indian Ocean BGC-Argo NetCDF corpus
in `backend/bgc data/`.

**Date:** 2026-11-23
**Dataset location:** `backend/bgc data/bgc raw data/incois/<WMO>/profiles/*.nc`
**Implementation under audit:** `data_processor.extract_bgc_data()` and the
`ArgoDataProcessor` ingestion path, unchanged.
**Changes made to application code:** none.
**Changes made to the dataset:** none (all files opened read-only).
**Full dataset ingested:** no.

> **Status after the follow-up fixes:** B1, B2, B3 and B5 have since been fixed
> (`backend/data_processor.py`, `backend/database_manager.py`, migration
> `V3__profile_identity_and_optional_position.sql`) and validated with a
> controlled 13-profile ingestion. Section 4 below documents the behaviour
> observed *before* those fixes; B4 and B6 were accepted as-is.

---

## 1. Dataset inventory

Produced by `backend/scripts/bgc_dataset_inventory.py` (metadata-only: file headers
and profile-level metadata, no measurement arrays materialised).

### 1.1 Volume and layout

| Property | Value |
|---|---|
| NetCDF files | **1,264** |
| Profiles inside those files (`N_PROF` summed) | **2,509** |
| Unreadable / corrupt files | **0** |
| DAC directories | **1** (`incois`) |
| Unique floats (WMO) | **8** |
| Sub-directory layout | `<dac>/<WMO>/profiles/<PREFIX><WMO>_<CYCLE>.nc` |

Per-float file counts: `2902306` 170, `2903951` 138, `3902581` 236,
`4903783` 206, `6990679` 52, `7902170` 184, `7902190` 114, `7902200` 164.

**Only one DAC is represented.** The brief expected "multiple DAC directories, where
available"; the downloaded subset has none, so cross-DAC behaviour is untested.

### 1.2 Filename prefixes

| Prefix | Meaning | Files | Profiles |
|---|---|---|---|
| `D` | core, delayed mode | 246 | 703 |
| `R` | core, real time | 386 | 1,174 |
| `SD` | **synthetic BGC**, delayed | 246 | 246 |
| `SR` | **synthetic BGC**, real time | 386 | 386 |
| `BD` / `BR` | real BGC | 0 | 0 |

The `SD`/`SR` counts are exactly equal to the `D`/`R` counts, and every one of the
626 unique `(float, cycle)` pairs appears **both** in a core file and in a synthetic
BGC file. The corpus is therefore two parallel copies of the same 626 profiles:
one core-only set and one core+BGC set.

Consequently BGC data lives **only** in `SD`/`SR` files, and filenames alone would
have been misleading in the other direction too: `D`/`R` files still contain
BGC-adjacent variables (`MTIME`, `*_STD`, `*_MED`), which is why the brief's
instruction to inspect contents rather than prefixes was correct.

### 1.3 Anomalies

1. **`N_PROF` is not "multiple cycles".** 619 files (49 %) contain more than one
   `N_PROF` entry (distribution: 645 files × 1, 303 × 2, 6 × 3, 310 × 4), and in
   **every** such file all `N_PROF` entries carry the **same** `CYCLE_NUMBER`.
   `N_PROF` is distinguishing ascending/descending samplings and parameter groups of
   one cycle, not separate cycles. This is the single most consequential finding —
   see B2.
2. **2,509 profiles collapse to 626 unique `(float, cycle)` pairs.**
   Multiplicity: 304 keys × 5, 303 × 3, 13 × 2, 6 × 9.
3. **`FORMAT_VERSION` differs by product:** all 1,877 core profiles report `3.1`;
   all 632 synthetic-BGC profiles report `1.0`.
4. **`DATA_MODE` is absent from every `SD`/`SR` file.** They carry
   `PARAMETER_DATA_MODE` only (see B4).
5. **Twelve files carry a trailing `D` in the filename**
   (`D3902581_001D.nc` … `SD3902581_006D.nc`), which breaks the
   `<PREFIX><WMO>_<CYCLE>.nc` convention. `D3902581_001D.nc` holds 3 `N_PROF`
   entries with **NaN latitude/longitude**; `SD3902581_001D.nc` holds 1 with NaN
   position. These are the only 4 profiles in the corpus without a position fix.
6. No duplicate filenames; no duplicate file contents.

### 1.4 Parameters present

Measured from `STATION_PARAMETERS`, not from variable names:

| Parameter | Profiles | Floats |
|---|---|---|
| `PRES` | 2,509 | all |
| `TEMP`, `PSAL` | 1,877 | all |
| `DOXY` | 619 | all 8 |
| `CHLA` | 619 | all 8 |
| `BBP700` | 619 | all 8 |
| `PH_IN_SITU_TOTAL` | 316 | 4 |
| `CHLA_FLUORESCENCE` | 316 | 4 |
| `NITRATE` | 126 | 2 (`2902306`, `7902190`) |
| `MTIME`, `*_STD`, `*_MED` | 626–1,258 | core files only |

All five requested BGC parameters are present. **126 profiles contain all five at
once** (floats `2902306` and `7902190`). `CHLA_FLUORESCENCE` is a sixth BGC
parameter outside the predefined list; the extractor discovers it dynamically.

### 1.5 Data modes

`PARAMETER_DATA_MODE` entries across the 632 synthetic-BGC profiles:
`D` 738, `R` 1,358, `A` 2,415 — all three Argo modes are represented.
Per target parameter: `CHLA` is always `A`; `NITRATE` and `PH_IN_SITU_TOTAL` are
always `R`; `DOXY` is `A` (303) or `R` (316); `BBP700` is `A` (316) or `R` (303).
Because `R` means "real-time, not adjusted", `NITRATE` has **no** adjusted arrays at
all — a genuine "missing adjusted data" case, exercised in the tests.

### 1.6 Geographic and temporal coverage

| Property | Value |
|---|---|
| Latitude | −11.57° … +22.10° |
| Longitude | 58.43° … 89.75° |
| Measurement levels (`N_LEVELS`) | 87 … 2,106 |
| Years | 2024: 671, 2025: 1,040, 2026: 798 profiles |

Consistent with an Arabian Sea / Bay of Bengal / equatorial Indian Ocean subset.
The brief said "primarily 2026"; in fact 2026 is 32 % of profiles.

---

## 2. Implementation audit

Classification vocabulary: **IMPLEMENTED** / **IMPLEMENTED BUT NOT VERIFIED WITH
REAL FILES** / **PARTIALLY IMPLEMENTED** / **NOT IMPLEMENTED** / **FAILED
VALIDATION**.

| # | Capability | Class | Evidence |
|---|---|---|---|
| 1 | BGC parameter discovery | IMPLEMENTED + verified | 5 targets found; `CHLA_FLUORESCENCE` found dynamically on 316 profiles |
| 2 | Known parameters (DOXY, CHLA, BBP700, NITRATE, PH_IN_SITU_TOTAL) | IMPLEMENTED + verified | all five extracted with value-exact match |
| 3 | Parameters outside the predefined list | IMPLEMENTED + verified | `CHLA_FLUORESCENCE` extracted, `*_dPRES` correctly ignored |
| 4 | `STATION_PARAMETERS` handling | IMPLEMENTED + verified | padded 64-char names, blank slots, case-folding all handled |
| 5 | `PARAMETER_DATA_MODE` | IMPLEMENTED + verified | 632 profiles; **0** length mismatches against `STATION_PARAMETERS` on the whole corpus |
| 6 | Raw measurements | IMPLEMENTED + verified | element-wise equal to source, 4-dp rounding (see B6) |
| 7 | Measurement QC | IMPLEMENTED + verified | equal to source, aligned by index |
| 8 | Adjusted measurements | IMPLEMENTED + verified | read from `<P>_ADJUSTED`, never copied from raw |
| 9 | Adjusted errors | IMPLEMENTED + verified | present where source has them, `None` otherwise |
| 10 | Adjusted QC | IMPLEMENTED + verified | same |
| 11 | Scientific calibration metadata | IMPLEMENTED + verified | real records returned (PRES/TEMP/PSAL/BBP700, 4,649 bytes) |
| 12 | Missing variables | IMPLEMENTED + verified | absent variables yield `None`, never fabricated |
| 13 | Fill values / NaN | IMPLEMENTED + verified | 99999 / 60000 / −99999 / NaN → `null`, lengths preserved |
| 14 | Different native measurement lengths | **IMPLEMENTED BUT NOT VERIFIED WITH REAL FILES** | every BGC parameter in this corpus shares one `N_LEVELS`; the differing-length path is covered only by the synthetic test |
| 15 | Profile-level vs measurement-level metadata | IMPLEMENTED + verified | `data_mode` profile-level, `PARAMETER_DATA_MODE` measurement-level, kept separate |
| 16 | Multi-profile NetCDF files | **PARTIALLY IMPLEMENTED** | extraction is correct per profile; **ingestion loses profiles** (B2) |
| 17 | Already-subsetted datasets (`isel(N_PROF=i)`) | IMPLEMENTED + verified | 0-D metadata scalars handled; exercised by every test |
| 18 | Core parameter regression | IMPLEMENTED + verified | PRES/TEMP/PSAL identical before and after BGC extraction |
| 19 | Profile-level BGC QC (`PROFILE_DOXY_QC`, …) | **NOT IMPLEMENTED** | only `PROFILE_PRES/TEMP/PSAL_QC` are read; no DB column exists for BGC ones |
| 20 | Ingestion of files without a position fix | **FAILED VALIDATION** | `IntegrityError 1048` (B1) |

Honest summary: the *extraction functions* are in good shape and matched the real
files exactly wherever the corpus exercises them. The *ingestion path* has two
defects that would silently or loudly lose data.

---

## 3. Real-file validation results

Ten files selected for float, prefix, parameter-set, length, data-mode and defect
coverage. Rationale is recorded inline in `backend/test_bgc_real_files.py`.

Column meanings — **Params** = BGC parameters extracted (core excluded);
**Levels** = `N_LEVELS`; **QC** / **Adjusted** / **Adj err** = how many extracted
parameters have that array; **Modes** = `PARAMETER_DATA_MODE` entries resolved.

| File | Float | Params | Target BGC found | Levels | QC | Adjusted | Adj err | Modes | Core |
|---|---|---|---|---|---|---|---|---|---|
| `SD2902306_003.nc` | 2902306 | 5 | all five | 1,033 | 5/5 | 5/5 | 5/5 | 8 | PASS |
| `SD7902190_019.nc` | 7902190 | 5 | all five | 1,036 | 5/5 | 5/5 | 5/5 | 8 | PASS |
| `SD7902170_001.nc` | 7902170 | 4 | DOXY,CHLA,BBP700,PH | 1,038 | 4/4 | 4/4 | 4/4 | 7 | PASS |
| `SR2903951_001.nc` | 2903951 | 4 | DOXY,CHLA,BBP700 | 174 | 4/4 | 4/4 | 4/4 | 7 | PASS |
| `SR7902200_015.nc` | 7902200 | 1 | PH only | 997 | 1/1 | 1/1 | 1/1 | 4 | PASS |
| `SD3902581_001.nc` | 3902581 | 4 | DOXY,CHLA,BBP700 | 735 | 4/4 | 4/4 | 4/4 | 7 | PASS |
| `SD3902581_001D.nc` | 3902581 | 4 | DOXY,CHLA,BBP700 | 1,860 | 4/4 | 4/4 | 4/4 | 7 | PASS* |
| `D3902581_001.nc` | 3902581 | 6 | none (core-derived only) | 494 | 6/6 | 0/6 | 0/6 | 0 | PASS* |
| `D2902306_001.nc` | 2902306 | 0 | none | 996 | – | – | – | 0 | PASS |
| `R6990679_001.nc` | 6990679 | 6 | none (core-derived only) | 93 | 6/6 | 0/6 | 0/6 | 0 | PASS |

`*` extraction passes; the file fails at ingestion (B1 for `SD3902581_001D`,
B2 for `D3902581_001`).

Additional verified facts:

- **Value equality.** Every extracted element equals the corresponding source value
  (rounded to 4 dp) after fill/NaN normalisation, for all 10 files and all `N_PROF`
  entries. No parameter present in `STATION_PARAMETERS` and in the file is dropped.
- **Sparse sampling.** In `SD7902190_019`, `DOXY` is non-null on 75 of 1,036 levels;
  the extractor returns 1,036 entries with 961 `null`s rather than a shorter array.
- **Adjusted ≠ raw.** `DOXY_ADJUSTED` is read from the adjusted variable. In file
  `SD7902190_019` the adjusted values happen to equal the raw values (mode `A`), so
  a "they must differ" assertion would have been wrong; the test instead compares
  against the source `_ADJUSTED` array.
- **Mode mapping.** Every `PARAMETER_DATA_MODE` flag matches the source for all
  sampled profiles; where the file has no such variable, `None` is returned.
- **Core regression.** `PRES`/`TEMP`/`PSAL` are byte-identical before and after
  `extract_bgc_data()`, and equal to the source. BGC extraction does not touch core
  arrays.

---

## 4. Bugs and defects discovered

### B1 — A profile without a position fix aborts the whole file — **High**

- **Files:** `incois/3902581/profiles/SD3902581_001D.nc` (1 profile),
  `incois/3902581/profiles/D3902581_001D.nc` (3 profiles).
- **Expected:** the profile is ingested with an unknown position, or skipped with a
  recorded reason.
- **Actual (live MySQL 8.0.46, disposable DB):**
  `mysql.connector.errors.IntegrityError: 1048 (23000): Column 'latitude' cannot be null`.
  `process_file` catches it and returns `error`, so **0 of 1** profile is stored.
- **Root cause:** `_process_single_profile` does `float(profile_ds["LATITUDE"].values)`
  and passes the resulting `nan` into an `INSERT` for a `FLOAT NOT NULL` column.
  There is no NaN/position guard.
- **Requires a code change:** yes — treat a missing position as skippable (or make
  the columns nullable and record `position_qc`), and never let one profile abort
  the file's remaining profiles.

### B2 — Sub-profiles of one cycle collide on `UNIQUE KEY (float_id, cycle_number)` — **High**

- **Files:** all 619 multi-`N_PROF` files, e.g. `D3902581_001.nc` (4 × cycle 1),
  `R6990679_001.nc` (4 × cycle 1).
- **Expected:** all `N_PROF` entries are stored.
- **Actual (live MySQL):** `{"inserted": 1, "skipped": 3}`. `check_profile_exists()`
  returns true for every sub-profile after the first, so they are counted as
  "already present" and dropped without warning.
- **Root cause:** `argo_profiles` has `UNIQUE KEY (float_id, cycle_number)`, but the
  dataset's profile identity is `(float, cycle, N_PROF)`. `insert_profile` is a plain
  `INSERT`; there is no upsert, and `profile_index_in_file` is not part of the key.
- **Impact if the corpus were ingested as-is:** 2,509 profiles → at most 626 rows
  (**1,245 profiles, 49.6 %, silently discarded**).
- **Requires a code change:** yes — widen the uniqueness to
  `(float_id, cycle_number, profile_index_in_file)` and make ingestion idempotent on
  that key.

### B3 — Core-derived variables are stored as "BGC parameters" — **Medium**

- **Files:** every core `D`/`R` file (619), e.g. `D3902581_001.nc`.
- **Expected:** `bgc_params` holds bio-geochemical measurements only.
- **Actual:** `bgc_params` = `{MTIME, TEMP_STD, PSAL_STD, PRES_MED, TEMP_MED, PSAL_MED}`.
  Stored JSON is **19,336 bytes** for that single core profile (versus 33,469 bytes for
  a genuine 5-parameter BGC profile). `MTIME` is a time offset, not a measurement.
- **Root cause:** dynamic discovery (deliberately) treats every `STATION_PARAMETERS`
  entry that is neither in `{PRES, TEMP, PSAL}` nor already known as a BGC parameter.
  There is no exclusion list for Argo's core auxiliary/derived families
  (`*_dPRES`, `MTIME`, `*_STD`, `*_MED`).
- **Requires a code change:** yes, a small one — exclude those families. This also
  affects any query layer that reads `bgc_params`.

### B4 — `data_mode` is `NULL` for all 632 synthetic-BGC profiles — **Low**

- **Files:** every `SD`/`SR` file.
- **Expected:** the profile-level mode, or an explicit "not provided".
- **Actual:** `extract_data_mode()` returns `None` because `DATA_MODE` is absent from
  those files (they carry `PARAMETER_DATA_MODE` only, at `FORMAT_VERSION` 1.0).
- **Assessment:** this is correct defensive behaviour — the code deliberately refuses
  to derive a profile mode from parameter modes — not a bug. It is recorded because
  a `NULL data_mode` on 25 % of rows must not later be read as "mode unknown/error".
  `PARAMETER_DATA_MODE` is fully populated for those profiles.

### B5 — BGC columns exist only through the V2 migration; otherwise data is dropped silently — **Medium**

- **Path:** `DatabaseManager.create_mysql_tables()` creates `argo_profiles` with only
  base columns (`pressure`, `temperature`, `salinity`, `bgc_params`). The remaining
  BGC columns come exclusively from
  `migrations/V2__add_qc_adjusted_provenance_and_bgc_columns.sql`.
- **Actual:** `insert_profile()` filters the payload by the columns it can see
  (`_get_profile_table_columns`), so on a database without V2 — or whenever
  `SHOW COLUMNS` fails and the hard-coded fallback set is used — `parameter_data_mode`,
  `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_error`, `bgc_adjusted_qc`, `scientific_calib`
  and the core QC/adjusted columns are **silently discarded**, with no error and no
  log line.
- **Requires a code change:** yes — fail loudly when an expected column is missing.
  (The current development database *does* have all 36 columns, so this is latent.)

### B6 — Values rounded to 4 decimals — **Low, by design**

`extract_measurement_array()` rounds every value to 4 dp, so the absolute error is
at most 5×10⁻⁵. That is negligible for `DOXY` (≈200 µmol/kg, relative error
≤2.5×10⁻⁷) but not for small `BBP700` values (≈0.001 m⁻¹, up to 5 % relative error).
Acceptable for a college project, but it must be stated if these numbers are ever
used quantitatively — and `BBP700` is exactly the parameter where it matters.

---

## 5. Test results

Run from `backend/`.

### 5.1 Existing tests

```
python -m unittest test_bgc_readiness -v
Ran 20 tests in 0.096s
OK
```

All 20 synthetic-fixture tests pass. They were **not** treated as evidence of
real-file compatibility — as the module's own docstring warns, every fixture is
constructed with `xarray.Dataset`.

### 5.2 New tests

`backend/test_bgc_real_files.py` — 23 tests, 6 classes:

| Class | Tests | What it establishes |
|---|---|---|
| `DatasetInventoryTests` | 3 | files are readable; all five targets present; core present everywhere |
| `ParameterExtractionTests` | 5 | no silent parameter drop; value equality; the all-five case; dynamic discovery; no invented parameters |
| `ArrayAlignmentTests` | 4 | QC/adjusted/error alignment; QC value equality; adjusted read from `_ADJUSTED`; mode equality |
| `MissingValueTests` | 5 | fill→null with preserved lengths; sparse sampling; missing position; absent `DATA_MODE`; core `DATA_MODE` |
| `CoreRegressionTests` | 2 | core values unchanged by BGC extraction; core-derived vs core separation |
| `IngestionPayloadTests` | 4 | serialisable payload; one payload per sub-profile; cycle-collision drop; NaN position recorded |

```
python -m unittest test_bgc_real_files -v
Ran 23 tests in 43.451s
OK
```

| Result | Count |
|---|---|
| Passed | 23 |
| Failed | 0 |
| Skipped | 0 (dataset present) |

Design properties: no network access; the dataset is opened read-only and never
modified; no raw NetCDF files are committed; the path is configurable via
`BGC_DATA_DIR` and the whole module **skips** if it is unset/absent, so CI without
the corpus stays green; only 10 of 1,264 files are touched.

Two tests are deliberate **characterisation tests** of current, defective behaviour
(`...cycle_collision_drops_duplicate_profiles`,
`...missing_position_fix_as_nan`). They are named so that fixing B1/B2 forces them
to be updated, rather than letting the bug become invisible.

### 5.3 Notes

- `xarray.open_dataset()` costs ~4.7 s per file on this machine while `netCDF4`
  opens the same file in ~16 ms. The tests therefore load each sample file once and
  cache it. At 4.7 s/file, ingesting 1,264 files through the current xarray-based
  reader would take roughly **1.6 hours** locally — an operational note, not a defect.
- `test_bgc_readiness` prints two `UserWarning`s ("Discarding nonzero nanoseconds")
  from `to_pydatetime()`; pre-existing and harmless.

---

## 6. Database readiness

Checked two ways: (a) read-only inspection of the development schema, (b) a
**disposable** database (`intelliquery_bgc_audit_tmp`, created, migrated, exercised
and dropped; development data untouched — still 1,384 profiles, 0 with BGC).

### 6.1 Representation

The development `argo_profiles` table has **36 columns**, including all BGC ones
from V2: `bgc_params`, `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_error`,
`bgc_adjusted_qc`, `parameter_data_mode`, `scientific_calib`, plus the core QC /
adjusted / provenance columns.

| Extracted data | Column | Round-trip verified |
|---|---|---|
| Raw BGC measurements | `bgc_params` JSON | yes — 33,469 bytes stored for a 5-parameter profile |
| BGC QC flags | `bgc_qc` JSON | yes |
| Adjusted values / errors / QC | `bgc_adjusted`, `bgc_adjusted_error`, `bgc_adjusted_qc` JSON | yes |
| Per-parameter modes | `parameter_data_mode` JSON | yes — `{"PRES":"D",…,"BBP700":"A"}` |
| Calibration records | `scientific_calib` JSON | yes |
| Provenance | `source_file`, `source_file_sha256`, `profile_index_in_file`, `source_metadata` | yes |

Serialisation is `json.dumps`; `null` is used for missing values, and no `NaN`
token reaches the JSON (asserted in the tests). Shapes and modes therefore survive
the round trip **as JSON**. Conclusion: **the schema can represent the extracted BGC
data correctly.**

### 6.2 Blockers

Representation is not the problem; identity and constraints are:

1. `latitude`/`longitude` are `FLOAT NOT NULL` → B1 fails at insert.
2. `UNIQUE KEY (float_id, cycle_number)` → B2 silently drops 49.6 % of profiles.
3. `create_mysql_tables()` does not create the BGC columns → B5 is a latent
   silent-loss path on any database built without running V2.

Also worth noting for storage sizing (measured `LENGTH()` after insert): a
1,033-level, 5-parameter profile stores **33,469 bytes** of `bgc_params` and
**31,414 bytes** of `bgc_adjusted`; in a 1,860-level file the same columns reached
~38 KB (`bgc_qc`) and ~44 KB (`bgc_adjusted_error`). Extrapolated naively to the 619
BGC profiles that is roughly 80–120 MB of JSON in `argo_profiles` — acceptable here,
but it is the reason B3's 19 KB of pseudo-BGC on core profiles is worth removing.

---

## 7. Recommended next step

**Fix the two ingestion blockers (B1, B2) and only then run a controlled ingestion
of 10 profiles.**

Rationale: extraction is validated — value-exact against 10 real files spanning all
five target parameters, all three data modes and the deepest/shallowest profiles in
the corpus. Running an ingestion now would be measuring the schema's constraints,
not the extractor: a 10-profile run would store fewer rows than expected (B2) and
would fail outright on any file without a position fix (B1), producing numbers that
look like extraction failures.

Concretely, in order:

1. Make profile identity `(float_id, cycle_number, profile_index_in_file)` and make
   insertion idempotent on it (B2).
2. Handle a missing `LATITUDE`/`LONGITUDE` without aborting the file (B1).
3. Re-run `test_bgc_real_files` — the two characterisation tests must be rewritten
   to assert the fixed behaviour.
4. Then ingest the same 10 sampled profiles and compare row counts and JSON content
   against the payloads these tests already produce.

B3 (exclude `MTIME`/`*_STD`/`*_MED`) and B5 (fail loudly on missing columns) are
small, independent follow-ups; they do not block the controlled ingestion.

Do **not** proceed to 50–100 profiles or full ingestion in this phase.

---

## Appendix — files created by this audit

| Path | Purpose |
|---|---|
| `backend/scripts/bgc_dataset_inventory.py` | metadata-only corpus inventory (`--json` writes a full per-profile dump) |
| `backend/test_bgc_real_files.py` | 23 real-file validation tests, skippable, 10-file sample |
| `docs/bgc/BGC_REAL_FILE_AUDIT.md` | this report |

Working notes (gitignored, not committed): `backend/scratch/bgc_inventory.json`,
`backend/scratch/bgc_db_probe.py` (disposable-database probe),
`backend/scratch/*_log.txt`.

Reproduce:

```bash
cd backend
python scripts/bgc_dataset_inventory.py
python -m unittest test_bgc_readiness -v
python -m unittest test_bgc_real_files -v
```
