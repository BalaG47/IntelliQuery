# IntelliQuery — Phase 4 Implementation Report: Data Explorer and Scientific Insights

**Date:** 2026-11-26  
**Scope:** Phase 4A (BGC-Aware Data Explorer) & Phase 4B (Scientific Insights)  
**Target Environment:** MySQL `argo_ocean_data_fresh`, Flask (`backend/explorer_api.py`), React + Vite + Plotly (`frontend/src`)

---

## 1. Initial Repository Audit

- **Frontend:** `frontend/src/Pages/DataExplorer.jsx` and `frontend/src/components/DataExplorer/*` already existed for Core ARGO exploration (`SummaryCards.jsx`, `FilterPanel.jsx`, `FloatsTable.jsx`, `ProfilesTable.jsx`, `ProfileDetailDrawer.jsx`) using `react-plotly.js` (`plotly.js-dist-min`), `axios`, and Tailwind theme tokens. `Navbar.jsx` already linked to `/insights`, but `App.jsx` had no `/insights` route and no Insights page existed.
- **Backend:** `backend/explorer_api.py` (`/api/data/*`, registered in `backend/ingestion.py`) and `backend/database_manager.py` implemented `/overview`, `/floats`, `/floats/<wmo>`, `/profiles`, `/profiles/<id>`, `/map-points`, and `/export`, but predated BGC integration (`get_data_overview()` hardcoded `"bgc": False`, `get_profiles_paginated()` had no BGC/direction/parameter filters, and `get_profile_by_id()` omitted `bgc_pressure`, `bgc_params`, `bgc_qc`, `bgc_adjusted`, and `data_mode`).
- **Database (`argo_ocean_data_fresh`):** Confirmed 30 floats and 2,016 profiles (`632` BGC-Argo, `1,384` Core-only; `2,010` ascending `'A'`, `6` descending `'D'`), with `bgc_params`, `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_qc`, `bgc_pressure`, `bgc_source_metadata`, and `parameter_data_mode` populated.

---

## 2. Existing Functionality Reused

- **`backend/bgc_calculations.py`:** `BGC_PARAMETER_UNITS`, `BGC_PARAMETER_DESCRIPTIONS`, `QC_POLICY_ALL_FINITE`, `extract_valid_bgc_series()`, and `calculate_bgc_statistics()`.
- **`backend/explorer_api.py` & `backend/database_manager.py`:** Existing parameterized SQL blueprint, `_parse_int`/`_parse_float`/`_parse_datetime` validators, and pagination patterns.
- **`frontend/src/components/DataExplorer/*`:** `Loading.jsx`, `ErrorState.jsx`, `EmptyState.jsx`, `Pagination.jsx`, `Navbar.jsx`, `Sidebar.jsx`, and `ThemeContext.jsx`.
- **Charting:** Existing `react-plotly.js` (`plotly.js-dist-min`) — no new charting library added.

---

## 3. Files Created and Modified

### Created
- `frontend/src/Pages/Insights.jsx`
- `backend/test_explorer_api_phase4a.py`
- `backend/test_insights_api_phase4b.py`
- `docs/PHASE_4_DATA_EXPLORER_INSIGHTS_IMPLEMENTATION_REPORT.md`

### Modified
- `backend/database_manager.py` (updated `get_data_overview`, `_build_profile_filters`, `get_profiles_paginated`, `get_profile_by_id`; added `get_insights_summary`, `get_insights_depth_profile`, `get_insights_compare`)
- `backend/explorer_api.py` (added `profile_type`, `parameter`, `direction` validation/filtering on `/profiles`; added `/insights/summary`, `/insights/depth-profile`, `/insights/compare`)
- `frontend/src/api.js` (added `getInsightsSummary`, `getInsightsDepthProfile`, `getInsightsCompare`)
- `frontend/src/App.jsx` (registered `/insights` route)
- `frontend/src/Pages/DataExplorer.jsx` (added `profile_type`, `parameter`, `direction` filter state)
- `frontend/src/components/DataExplorer/SummaryCards.jsx` (added Core vs BGC profile counts and available BGC parameter badges)
- `frontend/src/components/DataExplorer/FilterPanel.jsx` (added Profile Type, Parameter, Direction selectors and active chips; removed unused `activeTab` prop)
- `frontend/src/components/DataExplorer/ProfilesTable.jsx` (added Direction, Type badge, and BGC Parameters columns)
- `frontend/src/components/DataExplorer/ProfileDetailDrawer.jsx` (added accurate QC/data-mode banner, metadata fields, and `BgcSection` plotting BGC parameters against `bgc_pressure`)

---

## 4. API Endpoints and Response Structures

| Endpoint | Purpose | Key Request Params / Response Fields |
|---|---|---|
| `GET /api/data/overview` | Dataset overview | Adds `bgc_profiles`, `core_only_profiles`, `direction_counts`, `available_bgc_parameters` (`[{parameter, unit, profile_count}]`) |
| `GET /api/data/profiles` | Paginated profile browser | Adds optional `profile_type` (`all\|bgc\|core`), `parameter`, `direction` (`A\|D`), `sort_by=direction`; returns `direction`, `data_mode`, `has_bgc`, `profile_type`, `bgc_parameters` |
| `GET /api/data/profiles/<id>` | Profile detail | Preserves `measurements.pressure` (Core); adds separate `bgc_measurements` (`bgc_pressure`, per-param `values`, `qc`, `adjusted`, `adjusted_qc`, `unit`, `valid_count`, `missing_count`, `warnings`), `core_qc`, `qc_available` |
| `GET /api/data/insights/summary` | Dataset composition & BGC stats | Returns `composition`, `float_distribution`, `bgc_parameter_statistics` (via `calculate_bgc_statistics()`), `aggregation_note` |
| `GET /api/data/insights/depth-profile` | Binned BGC vs pressure | Params: `parameter`, `bin_size` (`10..500`, default `50`), optional `wmo_number`; returns `binning_method: "fixed_width_pressure_binning"`, `pressure_axis: "bgc_pressure"`, `bins` |
| `GET /api/data/insights/compare` | Multi-profile comparison | Params: `profile_ids` (2–5 IDs), `parameter`; returns `grid_note` and unmerged per-profile `values`/`pressures` arrays |

---

## 5. Data Explorer Implementation (Phase 4A)

- **Dataset Overview (`SummaryCards.jsx`):** Displays total floats (`30`), total profiles (`2,016`), BGC (`632`) vs Core-only (`1,384`) split, date range, geographic extents, and available BGC parameter badges with profile counts and units.
- **Profile Browser (`FilterPanel.jsx`, `ProfilesTable.jsx`):** Server-side pagination and filtering by WMO, Profile ID, Cycle, Profile Type (`BGC-Argo` / `Core Only`), Parameter (`DOXY`, `CHLA`, `CHLA_FLUORESCENCE`, `BBP700`, `NITRATE`, `PH_IN_SITU_TOTAL`, `TEMP`, `PSAL`, `PRES`), Direction (`A` / `D`), Date range, and Lat/Lon bounding box.
- **Profile Detail & Measurement Plot (`ProfileDetailDrawer.jsx`):** Displays core T/S/P charts alongside a dedicated `BgcSection` where users switch among parameters actually present in the profile, toggle Raw vs Adjusted (when available), inspect valid/missing counts and metadata warnings, and plot BGC measurements against `bgc_pressure` with a reversed downward depth axis and QC flags in hover tooltips.

---

## 6. Scientific Insights Implementation (Phase 4B)

- **Dataset Composition (`Insights.jsx`):** Core vs BGC distribution chart and stacked per-float WMO distribution across all 30 floats.
- **BGC Parameter Statistics:** Summary table for all 6 BGC parameters computed by `bgc_calculations.calculate_bgc_statistics()`, showing units, valid/missing counts, min, max, mean, median, std dev, QC policy (`all_finite_with_qc_reported`), and an explicit unbinned aggregation disclaimer.
- **Depth-Based Analysis:** Fixed-width pressure binning (`25`, `50`, `100`, `200 dbar`) over `bgc_pressure` with optional WMO filter, displaying bin count, profiles evaluated, and hover details (`min`/`max`/observation count/profile count).
- **Profile Comparisons:** Compares 2–5 profiles on their native pressure grids (`bgc_pressure` for BGC, `pressure` for Core) without cross-grid interpolation, and explicitly flags profiles lacking the selected parameter.

---

## 7. Scientific Correctness Measures

1. Core `pressure` and `bgc_pressure` are never merged (e.g., live profile `1386` has `998` core levels vs `1,035` BGC levels).
2. All BGC statistics reuse `bgc_calculations.py`, excluding `None`, `NaN`, `inf`, and fill values (`abs >= 99990.0`).
3. Units, `qc_policy`, `data_mode`, and `aggregation_scope` are explicitly displayed.
4. Depth axes use Plotly `autorange: "reversed"` so pressure increases downward.
5. Depth-binned analysis documents `fixed_width_pressure_binning` and bin width visibly.
6. Profile comparison never interpolates across mismatched pressure grids.

---

## 8. Performance Considerations

- List queries (`/api/data/profiles`) use `JSON_KEYS(p.bgc_params)` and `JSON_LENGTH(p.pressure)` in SQL and never load measurement arrays.
- Depth-profile analysis caps SQL scan at `LIMIT 500` profiles.
- Profile comparison restricts selection to `2..5` profile IDs.
- Existing `AbortController` cancellation is used on frontend filter/tab/parameter changes.

---

## 9. Test Results

- **Phase 4A + 4B API Unit Tests (`test_explorer_api_phase4a`, `test_insights_api_phase4b`):**  
  - **16 tests passed**, 0 failures, 0 errors (`EXIT_4AB=0`).
- **Live MySQL Smoke Check (`argo_ocean_data_fresh`):**  
  - Verified `get_data_overview`, `get_profiles_paginated`, `get_profile_by_id(1386)`, `get_insights_summary`, `get_insights_depth_profile("DOXY", 100)`, and `get_insights_compare([1386, 1387], "DOXY")` (`EXIT_SMOKE=0`).
- **Full Backend Regression + Phase 3 + Phase 4 Suite (19 test modules):**  
  - **686 tests run**, **OK (skipped=9)**, 0 failures, 0 errors (`EXIT_ALL=0`).
- **Frontend ESLint (`src/api.js`, `src/App.jsx`, `DataExplorer.jsx`, `Insights.jsx`, `DataExplorer/*.jsx`):**  
  - **0 errors**, 1 pre-existing `react-hooks/exhaustive-deps` warning in `DataExplorer.jsx` (`EXIT_LINT=0`).
- **Frontend Production Build (`npm run build`):**  
  - Verified in `task-1511` (see final response/log).

---

## 10. Known Limitations

- No dedicated Jest/Vitest DOM component test runner is installed in `frontend/package.json`; frontend verification relies on ESLint, Vite production build, and live API contract alignment.
- `get_insights_summary()` scans the 632 BGC profiles in MySQL to compute exact Python statistics via `bgc_calculations.py`; while fast for 632 profiles, larger datasets in future phases would benefit from precomputed summary caching.

---

## 11. Recommended Next Development Stage

- Add a lightweight frontend test runner (e.g., Vitest + React Testing Library) for automated UI regression testing of `DataExplorer.jsx` and `Insights.jsx`, and review Phase 4 before any Phase 5 work.
