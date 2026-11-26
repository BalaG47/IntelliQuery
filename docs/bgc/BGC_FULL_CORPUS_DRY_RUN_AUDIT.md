# IntelliQuery — BGC Phase 2: Full-Corpus Dry-Run Ingestion Audit

**Scope:** audit and verification only. No ingestion, no database write,
no NetCDF modification, no change to the matching algorithm.

**Date:** 2026-11-23  
**Dataset:** `C:\AI Projects\IntelliQuery\backend\bgc data\bgc raw data` (read-only)  
**Tool:** `backend/scripts/bgc_full_corpus_audit.py`  
**Architecture audited:** the unified core + BGC ingestion described in
`docs/bgc/BGC_PRODUCT_MATCHING.md` and implemented in
`backend/bgc_matching.py`, `backend/data_processor.py`,
`backend/database_manager.py`, migration V4.

---

## 1. Executive summary

* **1,264 files** and **2,509 `N_PROF` records** were indexed in **25.2 s** (whole audit 49.3 s).
* They resolve to **632 logical profiles** (`float, cycle, direction, profile_time`) across **8 floats** and **626 `(float, cycle)` groups**.
* **632 profiles** are merged core + BGC, **0** are core-only and **0** are BGC-only.
* **Duplicate logical identities: 0.** Missing `JULD`: 0. Unknown direction: 0.
* **1,245** processing variants are collapsed into their logical profile and preserved in `source_metadata.core_variants`.
* Pressure grids: **619 ok**, **13 `pres_scale_suspect`**, **0 `missing_pressure`**, **0 `grid_mismatch`**.
* **Verdict:** the ingestion plan is **safe to proceed with** — every profile has a distinct identity, every file was readable and no BGC axis contradicts its core profile. Rescaled pressure axes are flagged, never corrected; no profile is dropped.

| Measure | Value |
|---|---|
| Files scanned | 1,264 |
| `N_PROF` records | 2,509 |
| Logical profiles planned | 632 |
| Merged (core + BGC) | 632 |
| Core-only | 0 |
| BGC-only | 0 |
| Duplicate identities | 0 |
| Ascending / descending / unknown | 626 / 6 / 0 |
| `pres_scale_suspect` grids | 13 |
| Audit runtime | 49.3 s |

---

## 2. Dataset inventory

| File prefix | Files | `N_PROF` records | Product family |
|---|---|---|---|
| R | 386 | 1,174 | core |
| SR | 386 | 386 | BGC |
| D | 246 | 703 | core |
| SD | 246 | 246 | BGC |

* `N_PROF` records per file: 1 `N_PROF` -> 645 files, 2 `N_PROF` -> 303 files, 3 `N_PROF` -> 6 files, 4 `N_PROF` -> 310 files
* Valid `PRES` levels per core record: 2 – 1,995 (median 91) — counted per `N_PROF` record, so the sparse processing variants pull the median down; the selected variant of each profile is what gets stored.
* Valid `PRES` levels per BGC profile: 174 – 2,102 (median 997)
* Observation time range: 2024-01-12 to 2026-11-16

Profiles per float:

| Float | Files | Cycles | Logical profiles | Ascending | Descending | `pres_scale_suspect` |
|---|---|---|---|---|---|---|
| 2902306 | 170 | 85 | 85 | 85 | 0 | 10 |
| 2903951 | 138 | 69 | 69 | 69 | 0 | 0 |
| 3902581 | 236 | 112 | 118 | 112 | 6 | 0 |
| 4903783 | 206 | 103 | 103 | 103 | 0 | 0 |
| 6990679 | 52 | 26 | 26 | 26 | 0 | 0 |
| 7902170 | 184 | 92 | 92 | 92 | 0 | 2 |
| 7902190 | 114 | 57 | 57 | 57 | 0 | 0 |
| 7902200 | 164 | 82 | 82 | 82 | 0 | 1 |

Each file is opened once and reduced to metadata; measurement arrays are
counted, never retained. The index is written to
`backend/scratch/bgc_audit/index.json`.

---

## 3. Logical profile statistics

| Measure | Value |
|---|---|
| Source `N_PROF` records | 2,509 |
| Logical profiles planned | 632 |
| Duplicate logical identities | 0 |
| Profiles with missing `JULD` | 0 |
| Profiles with unknown direction | 0 |
| Matches relying on `JULD` only (no `DIRECTION`) | 0 |
| Records unusable before planning | 0 |
| Cycles holding both an ascending and a descending profile | 6 |

No duplicate logical identity exists: every planned profile maps to a
distinct `(float, cycle, direction, profile_time)`, so the unique key
would accept the whole plan.

632 logical profiles come from 626 `(float, cycle)` groups. The two counts differ because 6 cycles carry an ascending **and** a descending profile; the earlier investigation's "626" is the group count, not the profile count.

---

## 4. Matching statistics

| Match status | Profiles | Share |
|---|---|---|
| matched (core + BGC) | 632 | 100.0 % |
| core_only | 0 | 0.0 % |
| bgc_only | 0 | 0.0 % |

* `N_PROF` records accounted for by the plan: 2,509 of 2,509.
* Profiles with more than one matching BGC product: 0.
* Matching failures (BGC record without a core counterpart): 0.
* Ascending profiles: 626; descending: 6; unknown: 0.

Matching is done on `(JULD, DIRECTION)` inside a `(float, cycle)` group,
never on `profile_index_in_file`.

---

## 5. Core variant selection statistics

| Variants for one logical profile | Profiles |
|---|---|
| 1 variant(s) | 13 |
| 2 variant(s) | 303 |
| 3 variant(s) | 6 |
| 4 variant(s) | 310 |

| Measure | Value |
|---|---|
| Profiles with more than one variant | 619 |
| Variants collapsed into provenance | 1,245 |
| Profiles with a delayed-mode variant | 246 |
| Delayed-mode variant selected | 233 |
| Delayed-mode variant not selected | 13 |
| Selection rule: delayed >= 90 % of max levels | 233 |
| Selection rule: most levels | 399 |

Selected `DATA_MODE` distribution: `A` 179, `D` 233, `R` 220

Delayed-mode variants that were **not** selected (the documented rule
keeps the variant with most levels when the delayed record holds less
than 90 % of them; the adjusted values stay in the corpus and the
variant is listed in `core_variants`):

| Float | Cycle | Dir | Selected file | Mode | Levels | Variants (`#index levels mode`, `*` = selected) |
|---|---|---|---|---|---|---|
| 3902581 | 1 | A | D3902581_001.nc | R | 494 | #0 243lv D; #1 2lv R; #2 476lv R; #3 494lv R * |
| 3902581 | 1 | D | D3902581_001D.nc | R | 1775 | #0 224lv D; #1 1769lv R; #2 1775lv R * |
| 3902581 | 2 | A | D3902581_002.nc | R | 490 | #0 243lv D; #1 2lv R; #2 482lv R; #3 490lv R * |
| 3902581 | 2 | D | D3902581_002D.nc | R | 1995 | #0 224lv D; #1 1995lv R *; #2 1990lv R |
| 3902581 | 3 | A | D3902581_003.nc | R | 506 | #0 243lv D; #1 2lv R; #2 495lv R; #3 506lv R * |
| 3902581 | 3 | D | D3902581_003D.nc | R | 1729 | #0 223lv D; #1 1728lv R; #2 1729lv R * |
| 3902581 | 4 | A | D3902581_004.nc | R | 481 | #0 243lv D; #1 2lv R; #2 459lv R; #3 481lv R * |
| 3902581 | 4 | D | D3902581_004D.nc | R | 1906 | #0 226lv D; #1 1906lv R *; #2 1905lv R |
| 3902581 | 5 | A | D3902581_005.nc | R | 502 | #0 243lv D; #1 2lv R; #2 480lv R; #3 502lv R * |
| 3902581 | 5 | D | D3902581_005D.nc | R | 1856 | #0 222lv D; #1 1856lv R *; #2 1856lv R |
| 3902581 | 6 | A | D3902581_006.nc | R | 495 | #0 243lv D; #1 2lv R; #2 480lv R; #3 495lv R * |
| 3902581 | 6 | D | D3902581_006D.nc | R | 1755 | #0 223lv D; #1 1755lv R *; #2 1755lv R |
| 3902581 | 7 | A | D3902581_007.nc | R | 466 | #0 91lv D; #1 2lv R; #2 92lv R; #3 466lv R * |

---

## 6. Pressure-grid audit

| Grid status | BGC profiles | Share of BGC profiles |
|---|---|---|
| ok | 619 | 97.9 % |
| pres_scale_suspect | 13 | 2.1 % |
| missing_pressure | 0 | 0.0 % |
| grid_mismatch | 0 | 0.0 % |

BGC profiles audited: **632** (core-only profiles have no BGC axis to validate: 0).
Values are never rescaled, shifted or reordered by the audit or by
ingestion.

### `pres_scale_suspect` (13 profiles)

| Float | Cycle | Dir | BGC file | Core range (dbar) | BGC range (dbar) | Median ratio | Reason |
|---|---|---|---|---|---|---|---|
| 2902306 | 1 | A | SD2902306_001.nc | 5.97 – 1995.77 | 0.006 – 1.996 | 1000.01 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 11 | A | SD2902306_011.nc | 4.24 – 1995.95 | 0.004 – 1.996 | 998.981 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 16 | A | SD2902306_016.nc | 4.04 – 1997.27 | 0.004 – 1.997 | 1000.005 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 17 | A | SD2902306_017.nc | 4.11 – 1995.87 | 0.004 – 1.996 | 1000.0 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 18 | A | SD2902306_018.nc | 4.3 – 1995.95 | 0.004 – 1.996 | 999.99 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 23 | A | SD2902306_023.nc | 4.33 – 1997.3 | 0.004 – 1.997 | 1000.005 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 24 | A | SD2902306_024.nc | 4.2 – 1995.87 | 0.004 – 1.996 | 1000.02 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 25 | A | SD2902306_025.nc | 4.15 – 1995.4399 | 0.004 – 1.995 | 999.031 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 26 | A | SD2902306_026.nc | 4.35 – 1995.89 | 0.004 – 1.996 | 1000.0 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 2902306 | 39 | A | SR2902306_039.nc | 4.23 – 1997.3 | 0.004 – 1.997 | 1000.02 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 7902170 | 22 | A | SR7902170_022.nc | 4.23 – 1997.21 | 0.004 – 1.997 | 999.995 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 7902170 | 26 | A | SR7902170_026.nc | 4.19 – 1995.09 | 0.004 – 1.995 | 1000.0 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |
| 7902200 | 15 | A | SR7902200_015.nc | 4.2 – 1995.8199 | 0.004 – 1.996 | 999.95 | BGC pressure is approximately 1000x smaller than the core pressure of the same profile |

---

## 7. Parameter coverage

| Parameter | Profiles | Matched | BGC-only | QC | Adjusted | Adjusted error | Adjusted QC | `ok` grid |
|---|---|---|---|---|---|---|---|---|
| DOXY | 619 | 619 | 0 | 619 | 619 | 619 | 619 | 619 |
| CHLA | 619 | 619 | 0 | 619 | 619 | 619 | 619 | 619 |
| NITRATE | 126 | 126 | 0 | 126 | 126 | 126 | 126 | 126 |
| BBP700 | 619 | 619 | 0 | 619 | 619 | 619 | 619 | 619 |
| PH_IN_SITU_TOTAL | 316 | 316 | 0 | 316 | 316 | 316 | 316 | 303 |

Profiles per parameter combination (as delivered by the BGC product):

| Parameters in the profile | Profiles |
|---|---|
| BBP700, CHLA, CHLA_FLUORESCENCE, DOXY | 316 |
| BBP700, CHLA, DOXY, PH_IN_SITU_TOTAL | 177 |
| BBP700, CHLA, DOXY, NITRATE, PH_IN_SITU_TOTAL | 126 |
| PH_IN_SITU_TOTAL | 13 |

The 13 profiles whose only parameter is `PH_IN_SITU_TOTAL` are **exactly** the 13 `pres_scale_suspect` profiles: the same synthetic generation run delivered both a rescaled `PRES` axis and a reduced parameter set. Recorded as an observation, not as a causal claim.

---

## 8. Error and anomaly list

| Category | Count |
|---|---|
| Files that failed to open | 0 |
| Extraction errors | 0 |
| Matching errors | 0 |
| Plans with a warning | 26 |
| `pres_scale_suspect` | 13 |
| `grid_mismatch` | 0 |

Plans carrying a warning (the full list is in `plans.json`):

| Float | Cycle | Dir | Match | Warnings |
|---|---|---|---|---|
| 2902306 | 1 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 11 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 16 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 17 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 18 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 23 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 24 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 25 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 26 | A | matched | pressure grid: pres_scale_suspect |
| 2902306 | 39 | A | matched | pressure grid: pres_scale_suspect |
| 3902581 | 1 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 1 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 2 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 2 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 3 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 3 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 4 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 4 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 5 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 5 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 6 | A | matched | a delayed-mode variant was not selected |
| 3902581 | 6 | D | matched | a delayed-mode variant was not selected |
| 3902581 | 7 | A | matched | a delayed-mode variant was not selected |
| 7902170 | 22 | A | matched | pressure grid: pres_scale_suspect |
| 7902170 | 26 | A | matched | pressure grid: pres_scale_suspect |
| 7902200 | 15 | A | matched | pressure grid: pres_scale_suspect |

Warning kinds:

| Warning | Plans |
|---|---|
| pressure grid: pres_scale_suspect | 13 |
| a delayed-mode variant was not selected | 13 |

No anomaly required a change to the matching algorithm. Identity,
readability and matching produced no error at all; the warnings above are
the documented variant-selection rule and the known pressure-scale defect
of the synthetic product, each recorded per profile in `plans.json`.

---

## 9. Performance

| Measure | Value |
|---|---|
| Files scanned | 1,264 |
| `N_PROF` records | 2,509 |
| Index pass | 25.2 s |
| Planning pass | 0.1 s |
| Pressure/parameter verification pass | 23.5 s |
| Total wall clock | 49.3 s |
| Peak traced Python memory | 7.2 MiB |
| Files that failed to open | 0 |
| Extraction errors | 0 |
| Matching errors | 0 |
| Index parity vs production scanner | 61 records, 0 mismatches |

Index parity: the audit's own metadata scanner was compared field by
field with the production `bgc_matching.index_file` on a 30-file sample
(float, cycle, `JULD`, direction, data mode, level count, `N_PROF` index,
product family), so the audit is measuring the same index ingestion uses.

Memory stays bounded because only metadata is kept: the index holds one
small dictionary per `N_PROF` record, JSON is written once at the end, and
each `PRES` array is read inside a single `with netCDF4.Dataset(...)` block
and discarded immediately. Peak traced memory above covers Python
allocations only (the NetCDF library's own buffers are outside it).

---

## 10. Recommended next step

Proceed to a **bounded bulk ingestion of one complete float**, not the
whole corpus, against a disposable copy of the development database
first. Re-run it immediately afterwards to prove idempotency, then
compare its row count with the per-float plan in section 2.

No single float exercises both edge cases, so ingest two, in this order: **float 3902581** (118 profiles, 6 descending — the ascending/descending path), then **float 2902306** (10 of the 13 flagged grids — the pressure-defect path).

The 13 known rescaled-`PRES` files remain usable for BGC measurements but
must be excluded from any BGC-versus-depth analysis until the synthetic
product is regenerated.
