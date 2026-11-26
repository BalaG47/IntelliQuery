# BGC Query Pipeline — Phase 2.1 Retrieval Integrity Verification Report

**Date:** 2026-11-26  
**Database:** `argo_ocean_data_fresh` (2,016 profiles · 30 floats)  
**ChromaDB:** `backend/chroma_db_storage_fresh` / `argo_float_profiles` (2,016 vectors)  
**Verdict:** **PASS**

---

## 1. Files Inspected and Modified

### Inspected
- `backend/llmbackend.py`
- `backend/test_bgc_semantic_retrieval.py`
- Live ChromaDB collection `argo_float_profiles` metadata schema (`profile_id_sql`, `float_id_sql`, `cycle_number`, `direction`, `has_bgc`, `product_type`, `bgc_parameters`, `bgc_parameter_count`, `date`, `latitude`, `longitude`)
- Live MySQL parity across all 2,016 rows in `argo_profiles` / `argo_floats` (0 mismatches, 0 empty BGC parameter arrays)

### Modified
- `backend/llmbackend.py`
- `backend/test_bgc_semantic_retrieval.py`

### Created
- `docs/bgc/BGC_QUERY_PHASE_2_1_INTEGRITY_REPORT.md`

---

## 2. Exact Implementation Changes (`backend/llmbackend.py`)

1. **`_decode_sql_bgc_params(raw_bgc_params)`:** Safely decodes SQL `bgc_params` JSON into `(decoded_dict, is_malformed)`. Distinguishes legitimate SQL `NULL` (`(None, False)`) from malformed JSON, `"null"`, or non-dict values (`(None, True)`).
2. **`_sql_has_all_params(sql_row, required_params)`:** Authoritatively checks decoded SQL `bgc_params` keys (exact key membership, no substring matching) and ensures each requested parameter holds a non-empty list/tuple.
3. **`_requested_direction(parsed)` & `_combine_where_filters(bgc_where, required_direction)`:** Extracts `ProfileDirection` (`'A'` or `'D'`) from the parsed `QuerySpec` and combines it with `bgc_where` using ChromaDB's `$and` operator when both are present.
4. **`_check_metadata_sql_consistency(metadata, sql_row)`:** Expanded to verify all corresponding fields in the collection schema:
   - `profile_id_sql` vs `p.profile_id`
   - `float_id_sql` vs `f.wmo_number`
   - `cycle_number` vs `p.cycle_number`
   - `direction` vs `p.direction`
   - `bgc_params` JSON validity (`is_malformed`)
   - `has_bgc` vs non-empty SQL `bgc_params`
   - `bgc_parameters` token set vs decoded SQL `bgc_params` keys
5. **`_retrieve_semantic(..., required_direction=None)`:**
   - Uses ChromaDB `where` filtering for candidate narrowing (expanding `candidate_limit` when `required_params` or `required_direction` is set).
   - Runs `_check_metadata_sql_consistency()` before accepting any MySQL row; logs a warning and excludes mismatched candidates without modifying either store.
   - Enforces `_sql_has_all_params()` when `required_params` is set.
   - Enforces `sql_row["direction"] == required_direction` when `required_direction` is set.
   - Preserves semantic-distance ranking and deduplicates by `profile_id` (capped at 5).
6. **`answer_question()`:** Passes `required_direction` when explicitly requested; preserves the no-kwarg `_retrieve_semantic(question)` call for unrestricted queries.

---

## 3. SQL-Side Membership Verification Details

- ChromaDB's `bgc_parameters` metadata is used only to narrow candidates prior to MySQL lookup.
- Before any candidate enters `context_data`, `_sql_has_all_params()` decodes `p.bgc_params` via `json.loads` and verifies exact key presence and non-empty array content for every requested BGC parameter.
- Missing (`NULL`), empty (`{}`), or malformed `bgc_params` values always return `False` and never satisfy a parameter request.

---

## 4. Identity and Direction Consistency Behaviour

- **Profile & float identity:** `profile_id_sql` is checked against `p.profile_id`, `float_id_sql` against `f.wmo_number`, and `cycle_number` against `p.cycle_number`.
- **Direction filtering:** Explicit ascending (`'A'`) or descending (`'D'`) requests pass `{"direction": ...}` to ChromaDB (combined via `$and` when `has_bgc` is also present) and verify `p.direction` against the SQL row. Unrestricted queries leave `required_direction=None`.
- **Mismatch handling:** Any discrepancy between ChromaDB metadata and SQL (or between requested constraints and SQL) logs a `WARNING` and excludes the candidate; neither ChromaDB nor MySQL is modified.

---

## 5. New Test Cases and Results

Added `BgcPhase21IntegrityTests` (10 controlled fixture tests) and `BgcPhase21LiveDirectionTests` (2 live ChromaDB + MySQL tests) to `backend/test_bgc_semantic_retrieval.py`:

| # | Test Case | Result |
|---|---|---|
| 1 | ChromaDB claims DOXY, SQL lacks DOXY | PASS |
| 2 | SQL has DOXY, ChromaDB metadata lacks DOXY | PASS |
| 3 | SQL and ChromaDB disagree on `has_bgc` | PASS |
| 4 | Direction mismatch between request and SQL record | PASS |
| 5 | Float identity mismatch (`float_id_sql` vs `wmo_number`) | PASS |
| 6 | Malformed or null `bgc_params` | PASS |
| 7 | Multiple requested BGC params, partial SQL match | PASS |
| 8 | Semantic-distance order preserved after SQL verification | PASS |
| 9 | Unrestricted queries continue to work | PASS |
| 10 | Empty verified result does not fall back | PASS |
| Live-A | Live ascending BGC profile semantic retrieval | PASS |
| Live-D | Live descending BGC profile semantic retrieval | PASS |

**Full acceptance suite (`python -m unittest test_bgc_semantic_retrieval -v`):**
- **50 tests ran in 69.020s — `OK` (0 failures, 0 skipped), `EXIT_CODE=0`**

---

## 6. Regression Results and Exit Codes

**Command:**
```powershell
python -m unittest test_query_spec test_query_parser test_query_planner test_query_pipeline test_query_integration test_query_router test_query_boundary_4b2 test_query_reliability_4d test_query_reliability_4e test_query_remediation test_bgc_query_phase1 test_chromadb_bgc_metadata test_chromadb_vector_reconciliation
```

- **502 tests ran in 10.138s — `OK` (0 failures, 9 skipped), `EXIT_CODE=0`**
- The 9 skipped tests are pre-existing `INTELLIQUERY_LIVE_MYSQL=1` gated tests in `test_query_pipeline`.

---

## 7. Remaining Limitations

- Named ocean regions (e.g., `"Indian Ocean"`) still rely on semantic similarity rather than deterministic spatial bounding boxes.
- BGC scientific calculations, numeric BGC range filters, and UI/Qwen3 changes remain deferred to Phase 3.
- Concurrent multi-process access to the local persistent ChromaDB directory (`chroma_db_storage_fresh`) during parallel test runs can cause transient empty query results; running ChromaDB tests sequentially passes deterministically (`50/50`, `EXIT_CODE=0`).

---

## 8. Final Verdict

**PASS** — Authoritative SQL-side verification for BGC parameter membership, profile/float/cycle/direction identity consistency, and direction filtering is verified and complete.

---

## 9. Confirmed `float_id_sql` Identity Mapping

Inspected `backend/database_manager.py` (`_build_profile_metadata` line 730 and `sync_profile_vector` line 922) and compared live ChromaDB vectors (`chroma_db_storage_fresh` / `argo_float_profiles`) against MySQL (`argo_ocean_data_fresh`):

- `sync_profile_vector()` passes `float_id_db=record["wmo_number"]`, which `_build_profile_metadata()` stores as `"float_id_sql": int(float_id_db)`.
- Live record verification:
  - Core profile `profile_id_sql=1`: ChromaDB `float_id_sql=1900121` matches MySQL `argo_floats.wmo_number=1900121` (internal PK `argo_profiles.float_id=1`).
  - BGC profile `profile_id_sql=1385`: ChromaDB `float_id_sql=2902306` matches MySQL `argo_floats.wmo_number=2902306` (internal PK `argo_profiles.float_id=23`).
- `_check_metadata_sql_consistency()` in `backend/llmbackend.py` compares `metadata["float_id_sql"]` against `sql_row["wmo_number"]`, which is **correct** for the actual schema and stored values.

