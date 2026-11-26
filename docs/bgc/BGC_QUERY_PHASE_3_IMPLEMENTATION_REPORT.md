# IntelliQuery — Phase 3 Implementation Report: BGC Scientific Calculations and Grounded Query Responses

**Date:** 2026-11-26  
**Scope:** Phase 3 (3A Scientific Calculation Layer, 3B Numeric BGC Query Support, 3C Grounded Qwen3 Responses)  
**Target Environment:** MySQL `argo_ocean_data_fresh`, ChromaDB `backend/chroma_db_storage_fresh` (`argo_float_profiles`)

---

## 1. Executive Summary

Phase 3 extends IntelliQuery's existing deterministic and hybrid RAG query architecture to compute validated Biogeochemical-Argo (BGC-Argo) statistics in Python from authoritative MySQL measurements, filter profiles and floats by numeric BGC thresholds, and ground Qwen3 natural-language responses in Python-computed statistics rather than LLM-invented numbers.

All changes preserve the existing `query_router.py → query_parser.py → query_spec.py → query_pipeline.py → query_planner.py → query_integration.py` pipeline and `ArgoRAG.answer_question()` hybrid architecture.

---

## 2. Phase 3A — Validated BGC Scientific Calculation Layer

**File Created:** `backend/bgc_calculations.py`

### Key Components
- **`BgcCalculationResult` (dataclass):** Structured output containing `parameter`, `unit`, `aggregation_type`, `aggregation_scope`, `profile_id`, `wmo_number`, `cycle_number`, `direction`, `minimum`, `maximum`, `mean`, `median`, `std_dev`, `valid_count`, `missing_count`, `excluded_count`, `qc_policy`, `data_mode`, `provenance`, and `warnings`.
- **`extract_valid_bgc_series()`:**
  - Reads `bgc_params`/`bgc_qc` (raw) or `bgc_adjusted`/`bgc_adjusted_qc` (adjusted).
  - Pairs BGC observations exclusively with `bgc_pressure` (never core `pressure`).
  - Excludes `None`, `NaN`, `inf`, and NetCDF fill values (`abs(val) >= 99990.0`).
  - Supports `QC_POLICY_ALL_FINITE` (default; reports QC status without dropping unflagged values) and `QC_POLICY_GOOD_ONLY` (retains ARGO QC flags `1` and `2` only).
  - Propagates `bgc_source_metadata.grid_check == "pres_scale_suspect"` warnings.
- **`calculate_bgc_statistics()`:** Computes `minimum`, `maximum`, `mean`, `median`, and sample `std_dev` (when `valid_count >= 2`) across one or more SQL rows, emitting explicit warnings when aggregating across multiple profiles without depth grouping.
- **`filter_rows_by_bgc_threshold()`:** Evaluates numeric comparisons (`>`, `<`, `>=`, `<=`, `=`, `between`) across three scopes:
  - `measurement`: profile matches if at least one valid observation satisfies the condition.
  - `profile`: profile matches if its summary statistic satisfies the condition.
  - `float`: groups rows by `wmo_number`, computes the float-level metric, and returns qualifying float summary records.

---

## 3. Phase 3B — Numeric BGC Query Support

### Files Modified
- **`backend/query_spec.py`**:
  - Added `ComparisonOperator` enum (`>`, `<`, `>=`, `<=`, `=`, `between`).
  - Added `Aggregation.MEDIAN` and `Aggregation.STDDEV`.
  - Added `NumericFilter` dataclass (`parameter`, `operator`, `value`, `upper_value`, `scope`, `aggregation`, `unit`).
  - Added `numeric_filter: NumericFilter | None = None` to `QuerySpec`, `validate_spec()`, and the `CONVERSATIONAL` filter check.
  - Updated `COMPARE` intent validation to allow single BGC parameter comparisons.
- **`backend/query_parser.py`**:
  - Added `_BGC_EXTRA_AGGREGATION_ALIASES` for BGC `MEDIAN` and `STDDEV`, while keeping core-parameter median/stddev refusals intact.
  - Added `_extract_numeric_filter()` to parse BGC thresholds (`exceeds`, `above`, `below`, `at least`, `at most`, `between ... and ...`) and distinguish `measurement`, `profile`, and `float` scopes.
  - Updated `_detect_intent()` to route queries with `numeric_filter` to `Intent.LIST_PROFILES`.
- **`backend/query_planner.py`**:
  - Added `PlanOperation.BGC_NUMERIC_FILTER`, `PlanOperation.BGC_CALCULATION`, and `ResultShape.BGC_CALCULATION`.
  - Added `_plan_bgc_numeric_filter()` and `_plan_bgc_calculation()` with bounded `MAX_AGGREGATE_PROFILE_SCAN` SQL queries selecting `bgc_pressure`, `bgc_params`, `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_qc`, and `bgc_source_metadata`.
  - Updated `_has_any_filter()` to include `numeric_filter`.
- **`backend/query_pipeline.py`**:
  - Added post-processing in `run_pipeline()` for `bgc_numeric_filter`, `bgc_compare`, and `bgc_calculate` plan tags using `bgc_calculations`.
- **`backend/query_integration.py`**:
  - Added `result_shape` to `DeterministicAnswer`.
  - Added `_render_bgc_calculation()` and registered `bgc_calculation` in `_render()`.
  - Updated `has_structured_refinement()` to include `spec.numeric_filter is not None`.
- **`backend/test_query_spec.py`**:
  - Updated `test_unsupported_aggregation_is_rejected` to test `"PERCENTILE"` instead of `"MEDIAN"`, since `MEDIAN` is now a valid `Aggregation` enum member for BGC queries.

---

## 4. Phase 3C — Grounded Qwen3 Responses & Guard Integration

**File Modified:** `backend/llmbackend.py`
- Extended `_retrieve_semantic()` SQL projection to fetch `bgc_qc`, `bgc_adjusted`, `bgc_adjusted_qc`, and `data_mode`.
- Extended `_profile_summary()` to compute per-parameter statistics via `calculate_bgc_statistics()` and attach `summary["bgc_stats"]`.
- Extended `_build_compact_context()` to emit validated BGC statistics (`valid_count`, `missing_count`, `unit`, `qc_policy`, `min`, `max`, `mean`, `median`, `std_dev`, and QC/pressure warnings) into the Qwen3 prompt context.
- Added Qwen3 grounding rule 10 requiring exact reporting of context-supplied BGC statistics without recomputation, and renumbered subsequent rules (`11`, `12`).
- Updated `answer_question()` so successfully executed deterministic answers with `result_shape` in `("bgc_calculation", "bgc_numeric_filter")` bypass `_unsupported_analysis_answer()`, while preserving the guard for non-BGC/spurious executions.

---

## 5. Scientific Cross-Check: NetCDF vs. MySQL

Verified `calculate_bgc_statistics()` against raw NetCDF source `backend/bgc data/bgc raw data/incois/2902306/profiles/SD2902306_002.nc` (WMO `2902306`, cycle `2`, `profile_id=1386`, `direction=A`):

| Parameter | Source | Valid Count | Min | Max | Mean | Median |
|---|---|---:|---:|---:|---:|---:|
| `DOXY` | NetCDF | 71 | 1.7077 | 254.704 | 153.28 | 233.7224 |
| `DOXY` | MySQL (`bgc_calculations`) | 71 | 1.7077 | 254.704 | 153.2799 | 233.7224 |
| `CHLA` | NetCDF | 71 | 0.0292 | 0.7173 | 0.1005 | 0.0657 |
| `CHLA` | MySQL (`bgc_calculations`) | 71 | 0.0292 | 0.7173 | 0.1005 | 0.0657 |

---

## 6. Verification & Test Results

### New Phase 3 Test Suites
Command:
```powershell
cd "c:\AI Projects\IntelliQuery\backend"
python -m unittest test_bgc_calculations test_bgc_phase3b_queries test_bgc_phase3c_responses -v
```
- **`test_bgc_calculations.py`** (Phase 3A): 56 tests — `OK` (`EXIT_3A=0`)
- **`test_bgc_phase3b_queries.py`** (Phase 3B): 34 tests — `OK`
- **`test_bgc_phase3c_responses.py`** (Phase 3C): 28 tests — `OK`
- **Combined New Phase 3 Tests:** **118 tests passed**, 0 failures, 0 errors (`EXIT_NEW=0`)

### Full Regression Suite
Command:
```powershell
cd "c:\AI Projects\IntelliQuery\backend"
python -m unittest test_query_spec test_query_parser test_query_planner test_query_pipeline test_query_integration test_query_router test_query_boundary_4b2 test_query_reliability_4d test_query_reliability_4e test_query_remediation test_bgc_query_phase1 test_chromadb_bgc_metadata test_chromadb_vector_reconciliation test_bgc_semantic_retrieval
```
- **Result:** **552 tests run**, **OK (skipped=9)**, 0 failures, 0 errors (`EXIT_REGRESSION=0`)

---

## 7. Files Created & Modified

### Created
- `backend/bgc_calculations.py`
- `backend/test_bgc_calculations.py`
- `backend/test_bgc_phase3b_queries.py`
- `backend/test_bgc_phase3c_responses.py`
- `docs/bgc/BGC_QUERY_PHASE_3_IMPLEMENTATION_REPORT.md`

### Modified
- `backend/query_spec.py`
- `backend/query_parser.py`
- `backend/query_planner.py`
- `backend/query_pipeline.py`
- `backend/query_integration.py`
- `backend/llmbackend.py`
- `backend/test_query_spec.py`

---

## 8. Known Issues / Notes
- Multi-profile BGC aggregations compute statistics across all valid observations in the bounded scan (`MAX_AGGREGATE_PROFILE_SCAN`) without depth binning; an explicit warning is attached to `BgcCalculationResult.warnings`.
- Live Ollama/Qwen3 inference tests in the existing suite log connection errors when Ollama is not running locally; Qwen3 prompt grounding and context construction are verified via unit tests.
- Phase 3 is complete. Data Explorer and Insights integration (Phase 4) has not been started.
