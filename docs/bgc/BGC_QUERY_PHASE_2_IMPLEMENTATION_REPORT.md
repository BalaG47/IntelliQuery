# BGC Query Pipeline — Phase 2 Implementation Report

**Date:** 2026-11-26  
**Database:** `argo_ocean_data_fresh`  
**ChromaDB:** `backend/chroma_db_storage_fresh` (collection: `argo_float_profiles`)  
**Baseline:** 2,016 profiles (1,384 core-only · 632 BGC) · 30 floats · 2,016 ChromaDB vectors

---

## 1. Files Inspected

- `backend/llmbackend.py` (`ArgoRAG.answer_question()`, `_retrieve_semantic()`, `_profile_summary()`, `_build_compact_context()`, `_answer_with_query_spec()`)
- `backend/database_manager.py` (`chroma_client`, `chroma_collection`, `embedding_model`, `mysql_engine`)
- `backend/query_router.py` (`ArgoQueryRouter.route()`, `Route.CHROMA_SEMANTIC`)
- `backend/query_integration.py` (`answer_deterministically()`, `has_structured_refinement()`)
- `backend/query_spec.py` (`BGC_PARAMETERS`, `Parameter`, `QuerySpec`)
- `backend/query_parser.py` (`parse_query()`)
- `backend/test_query_integration.py`
- `backend/test_query_boundary_4b2.py`

---

## 2. Files Modified

| File | Change summary |
|---|---|
| `backend/llmbackend.py` | Added `parse_query`, `BGC_PARAMETERS`, `_BGC_PARAM_TOKENS`; added `_bgc_where_filter()`, `_requested_bgc_params()`, `_metadata_has_param()`, `_check_metadata_sql_consistency()`; extended `_retrieve_semantic()` with ChromaDB `where` filtering, exact parameter membership filtering, expanded SQL columns (`bgc_pressure`, `bgc_source_metadata`, `direction`), duplicate suppression, and metadata/SQL consistency checks; updated `answer_question()` to pass BGC filter/parameter context; extended `_profile_summary()` and `_build_compact_context()` with BGC metadata, `bgc_pressure`, and per-parameter valid counts |
| `backend/test_query_boundary_4b2.py` | Updated mock assertion in `test_discovery_questions_reach_semantic_retrieval` to check `assert_called_once()` and `call_args[0][0] == question` when `required_params` is passed for oxygen discovery queries |

**File created:**

| File | Purpose |
|---|---|
| `backend/test_bgc_semantic_retrieval.py` | 38 acceptance tests (17 helper unit tests, 16 live ChromaDB/MySQL tests, 5 controlled stub edge-case tests) |

---

## 3. Actual Retrieval Architecture

The existing hybrid deterministic-first architecture is preserved:

```
ArgoRAG.answer_question(question)
  ├─ ArgoQueryRouter.route(question)
  ├─ Exact MySQL routes (MYSQL_PROFILE_COUNTS, MYSQL_WMO_PROFILE_COUNT, ...)
  └─ Else (semantic discovery / structured fallback):
       ├─ Deterministic QuerySpec pipeline (_answer_with_query_spec)
       ├─ Unsupported-analysis guard (_unsupported_analysis_answer)
       └─ Semantic fallback:
            ├─ parse_query(question) -> _bgc_where_filter + _requested_bgc_params
            ├─ _retrieve_semantic(question, bgc_where=..., required_params=...)
            │    ├─ Embed question with SentenceTransformer('all-MiniLM-L6-v2')
            │    ├─ chroma_collection.query(..., where=bgc_where, n_results=...)
            │    ├─ Exact BGC token filter against bgc_parameters metadata
            │    ├─ Authoritative MySQL lookup by profile_id_sql
            │    ├─ Metadata/SQL consistency verification (_check_metadata_sql_consistency)
            │    └─ Deduplicated, rank-preserved context_data (capped at 5)
            └─ _profile_summary() -> _build_compact_context() -> Qwen3 / direct answer
```

No separate BGC retrieval engine or secondary ChromaDB collection was created.

---

## 4. Metadata Filtering Implementation (Task A)

`_bgc_where_filter(parsed)` derives the ChromaDB `where` clause strictly from the validated `QuerySpec`:

- **BGC-only requests** (`spec.has_bgc is True`): passes `where={"has_bgc": True}` to `chroma_collection.query()`.
- **Core-only requests** (`spec.has_bgc is False`): passes `where={"has_bgc": False}`.
- **Unrestricted requests** (`spec.has_bgc is None`): passes no `where` clause (`bgc_where=None`), preserving the original unrestricted retrieval path.

In `answer_question()`, when neither `_bgc_where` nor `_required_params` is set, `_retrieve_semantic(question)` is invoked with no extra keyword arguments, keeping unrestricted semantic retrieval identical to Phase 1.

---

## 5. Parameter Membership Filtering (Task B)

ChromaDB stores `bgc_parameters` as a sorted, comma-separated string (e.g. `"BBP700,CHLA,DOXY,PH_IN_SITU_TOTAL"` or `"BBP700,CHLA,CHLA_FLUORESCENCE,DOXY"`). Substring matching cannot safely distinguish `CHLA` from `CHLA_FLUORESCENCE`.

- `_requested_bgc_params(parsed)` extracts only parameters belonging to `BGC_PARAMETERS` (`DOXY`, `CHLA`, `BBP700`, `NITRATE`, `PH_IN_SITU_TOTAL`, `CHLA_FLUORESCENCE`), ignoring core parameters (`TEMP`, `PSAL`, `PRES`).
- `_metadata_has_param(metadata, param_token)` splits `bgc_parameters` on `","`, strips whitespace, and checks exact set membership (`param_token in tokens`).
- When `required_params` is non-empty, `_retrieve_semantic()` expands the ChromaDB candidate window (`max(100, min(collection.count(), 1000))`) so rarer parameters such as `NITRATE` and `PH_IN_SITU_TOTAL` are not missed by a top-5 cutoff, then filters candidates in ChromaDB semantic-distance order and caps the final MySQL-verified `context_data` at 5 profiles.
- If no candidates satisfy `required_params`, `_retrieve_semantic()` returns `[]` rather than falling back to core-only or non-matching profiles.

---

## 6. MySQL BGC Context Retrieval (Task C)

The SQL query in `_retrieve_semantic()` now selects:

- Existing core fields: `p.profile_id`, `p.cycle_number`, `p.profile_time`, `p.latitude`, `p.longitude`, `p.pressure`, `p.temperature`, `p.salinity`, `p.bgc_params`, `f.wmo_number`
- Added V4/BGC fields: `p.bgc_pressure`, `p.bgc_source_metadata`, `p.direction`

`_profile_summary()` and `_build_compact_context()` expose:
- `direction`, `product_type`, `bgc_parameters`, `bgc_parameter_count`
- `bgc_pressure_count`, `bgc_pressure_min`, `bgc_pressure_max` (kept strictly separate from core `pressure`)
- Per-parameter valid observation counts decoded safely from `bgc_params` JSON (`bgc_param_counts`) without fabricating missing values or modifying raw arrays.

---

## 7. Metadata Consistency Handling (Task D)

`_check_metadata_sql_consistency(metadata, sql_row)` verifies each candidate before including it:
- Compares ChromaDB `has_bgc` against whether SQL `bgc_params` is non-null/non-empty.
- Compares ChromaDB `cycle_number` against SQL `cycle_number`.
- If a mismatch is detected, or if `profile_id_sql` does not exist in MySQL, a warning is logged and the inconsistent record is excluded from `context_data`.
- `seen_profile_ids` guarantees each SQL `profile_id` appears at most once.

---

## 8. Acceptance Test Results

**Suite:** `backend/test_bgc_semantic_retrieval.py`  
**Command:** `python -m unittest test_bgc_semantic_retrieval -v`  
**Result:** `Ran 38 tests in 62.487s — OK` (0 failures, 0 skipped, `EXIT_CODE=0`)

Coverage across all 18 required acceptance cases:
- Pure-logic helper tests (`BgcHelperMethodTests`, 17 tests): exact token membership (`CHLA` vs `CHLA_FLUORESCENCE`), `_bgc_where_filter`, `_requested_bgc_params`, and `_check_metadata_sql_consistency`.
- Live ChromaDB + MySQL tests (`BgcSemanticRetrievalLiveTests`, 16 tests): BGC-only (`has_bgc=True`), core-only (`has_bgc=False`), unrestricted retrieval, all 6 BGC parameters (`DOXY`, `CHLA`, `CHLA_FLUORESCENCE`, `NITRATE`, `PH_IN_SITU_TOTAL`, `BBP700`), `direction`, `bgc_pressure` presence and separation from core `pressure`, non-fabrication of missing BGC values, empty fallback behavior, SQL/vector ID consistency, and deduplication.
- Controlled fixture tests (`BgcSemanticRetrievalStubTests`, 5 tests): metadata/SQL mismatch exclusion with warning log, semantic ranking preservation, missing SQL row handling, empty parameter-filter result, and duplicate candidate deduplication.

---

## 9. Regression Test Results

**Command:**
```powershell
python -m unittest test_query_spec test_query_parser test_query_planner test_query_pipeline test_query_integration test_query_router test_query_boundary_4b2 test_query_reliability_4d test_query_reliability_4e test_query_remediation test_bgc_query_phase1 test_chromadb_bgc_metadata test_chromadb_vector_reconciliation
```

**Result:** `Ran 502 tests in 10.583s — OK (skipped=9)`, `EXIT_CODE=0`  
All 13 modules passed with 0 failures. The 9 skipped tests are pre-existing `INTELLIQUERY_LIVE_MYSQL=1` gated tests in `test_query_pipeline`.

---

## 10. Before / After Retrieval Examples

- **Before Phase 2:** Querying `"Find BGC profiles with dissolved oxygen measurements."` via `_retrieve_semantic()` ran an unfiltered `n_results=5` ChromaDB query, which could return core-only profiles (`has_bgc=False`) with higher embedding similarity and omitted `bgc_pressure`, `bgc_source_metadata`, and `direction` from the SQL fetch.
- **After Phase 2:** `_bgc_where_filter()` supplies `where={"has_bgc": True}`, `_requested_bgc_params()` enforces exact `DOXY` token membership on `bgc_parameters`, MySQL returns `bgc_params`, `bgc_pressure`, `bgc_source_metadata`, and `direction`, and `_check_metadata_sql_consistency()` validates SQL/ChromaDB parity.

---

## 11. Remaining Geographic Limitations

Named ocean region queries (e.g., `"Find BGC profiles in the Indian Ocean."`) still rely on semantic similarity over ChromaDB document text rather than deterministic geographic bounding boxes. Phase 2 ensures that BGC-restricted regional queries only return BGC-enabled profiles, but semantic similarity alone does not prove geographic membership. Spatial bounding-box resolution remains deferred to a future phase.

---

## 12. Known Issues

- None blocking Phase 2.
- `test_query_boundary_4b2.py` required updating one mock assertion (`assert_called_once_with(question)`) because oxygen discovery queries now pass `required_params=frozenset({'DOXY'})` to `_retrieve_semantic()`.

---

## 13. Recommended Phase 3 Implementation Sequence

1. **BGC scientific calculations & direct answers:** Extend `_build_direct_profile_answer()` / scientific helpers in `llmbackend.py` for BGC parameters using `bgc_pressure` (kept separate from core `pressure`).
2. **BGC numeric range queries:** Extend `QuerySpec`, `query_parser.py`, and `query_planner.py` for numeric comparisons on BGC parameters.
3. **Named ocean region bounding boxes:** Add deterministic spatial bounds for named ocean basins where appropriate.
4. **Qwen3 prompt & Data Explorer integration:** Surface BGC parameter summaries and visualizations in the UI after explicit authorization.
