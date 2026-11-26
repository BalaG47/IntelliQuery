# BGC Query Pipeline — Phase 1 Implementation Report

**Date:** 2026-11-26  
**Database:** `argo_ocean_data_fresh`  
**Baseline:** 2,016 profiles (1,384 core · 632 BGC) · 30 floats · 2,016 ChromaDB vectors

---

## 1. Scope

Phase 1 extends the existing deterministic query pipeline to understand BGC-specific natural-language questions. No new query engine was introduced. The existing pipeline route

```
query_router → query_parser → query_spec → query_pipeline → query_planner → query_integration
```

was extended in-place.

**Not in scope (Phase 2):**
- Metadata-filtered ChromaDB retrieval for BGC queries
- BGC scientific calculations in `llmbackend.py`
- Qwen3 prompt modifications
- Data Explorer BGC visualisations

---

## 2. Files Modified

| File | Change summary |
|---|---|
| `backend/query_spec.py` | `CHLA_FLUORESCENCE` parameter; `ProfileDirection` enum; `has_bgc`, `direction`, `float_level` fields on `QuerySpec`; validation rules |
| `backend/query_parser.py` | `CHLA_FLUORESCENCE` alias (before `CHLA`); `_extract_bgc_filter_and_direction()`; `float_level` extraction; updated `_detect_intent` |
| `backend/query_planner.py` | `CHLA_FLUORESCENCE` in `_BGC_PARAMETER_KEYS`; `has_bgc` / `direction` SQL clauses; float-level DISTINCT query; `p.direction` in list-profiles SELECT |
| `backend/query_integration.py` | `has_structured_refinement()` updated; `_render_float_rows(rows, result)` call-site fixed to `_render_float_rows(rows)` |
| `backend/test_query_spec.py` | `len(BGC_PARAMETERS)` updated from 5 → 6; `CHLA_FLUORESCENCE` added to expected value set |

**File created:**

| File | Purpose |
|---|---|
| `backend/test_bgc_query_phase1.py` | 20 acceptance tests (10 parser, 10 live-database pipeline) |

---

## 3. Changes in Detail

### 3.1 `query_spec.py`

**`Parameter` enum** — added `CHLA_FLUORESCENCE = "CHLA_FLUORESCENCE"` as the sixth BGC parameter.

**`BGC_PARAMETERS` frozenset** — now contains all six members:
`{DOXY, CHLA, BBP700, NITRATE, PH_IN_SITU_TOTAL, CHLA_FLUORESCENCE}`.

**`ProfileDirection` enum** — new:
```python
class ProfileDirection(str, Enum):
    ASCENDING  = "A"
    DESCENDING = "D"
```

**`QuerySpec` dataclass** — three new optional fields:
```python
has_bgc:    bool | None           = None   # True=BGC-only, False=core-only, None=all
direction:  ProfileDirection | None = None  # ascending / descending filter
float_level: bool                 = False   # PARAMETER_AVAILABILITY returns floats, not profiles
```

**`validate_spec()`** — four new validation rules:
- `has_bgc` must be `bool` or `None`.
- `has_bgc=False` cannot be combined with BGC parameters.
- `direction` coerced through `_coerce_enum(spec.direction, ProfileDirection)`.
- `float_level` must be `bool`.

### 3.2 `query_parser.py`

**`_PARAMETER_ALIASES`** — `CHLA_FLUORESCENCE` inserted **before** `CHLA` to prevent "chlorophyll fluorescence" being partially consumed by the CHLA pattern.

**`_extract_parameters()`** — masks `CHLA_FLUORESCENCE` text before the CHLA pass, ensuring the two parameters are always distinguished.

**`_extract_bgc_filter_and_direction(text, ex)`** — new extraction function that sets:
- `ex.has_bgc = True` for "BGC", "biogeochemical" etc.
- `ex.has_bgc = False` for "core-only", "non-BGC", "physical-only" etc.
- `ex.direction` for "ascending" / "descending".

**`_detect_intent`** — updated to:
- Include `wmo` in the "which floats/wmos" pattern for `PARAMETER_AVAILABILITY`.
- Include all six BGC parameter aliases in the "containing" pattern.
- Set `ex.float_level = True` when "floats" or "wmos" appears without "profiles".
- Trigger `LIST_PROFILES` when `has_bgc is not None` or `direction is not None` and no higher-priority intent was detected.

### 3.3 `query_planner.py`

**`_BGC_PARAMETER_KEYS`** — `CHLA_FLUORESCENCE: "CHLA_FLUORESCENCE"` added.

**`_has_any_filter()`** — now returns `True` for `spec.has_bgc is not None` and `spec.direction is not None`.

**`_profile_filters()`** — two new SQL clauses:

```sql
-- has_bgc = True
JSON_LENGTH(COALESCE(p.bgc_params, '{}')) > 0

-- has_bgc = False
(p.bgc_params IS NULL OR JSON_LENGTH(p.bgc_params) = 0)

-- direction = 'A' or 'D'
p.direction = :profile_direction
```

**Float-level `PARAMETER_AVAILABILITY`** — when `spec.float_level` is `True`, generates:
```sql
SELECT DISTINCT f.float_id, f.wmo_number, f.project_name, f.platform_type
FROM argo_floats f
JOIN argo_profiles p ON f.float_id = p.float_id
<WHERE bgc_params JSON filter for the requested parameter>
ORDER BY f.wmo_number ASC
LIMIT :limit
```
Returns `ResultShape.FLOAT_ROWS` so `_render_float_rows()` formats the WMO list.

**`_plan_list_profiles` SELECT** — `p.direction` added to the standard column list so direction-filter results carry the field for verification.

### 3.4 `query_integration.py`

**`has_structured_refinement()`** — returns `True` when `spec.has_bgc is not None`, `spec.direction is not None`, or `spec.float_level`, preventing these questions from falling back to the legacy WMO route.

**`_render_float_rows` call site** — corrected from `_render_float_rows(rows, result)` to `_render_float_rows(rows)`.

---

## 4. Database Semantics

Verified against `argo_ocean_data_fresh`:

| Predicate | Count | Notes |
|---|---:|---|
| `bgc_params IS NOT NULL` | 632 | Exactly equals BGC=True |
| `bgc_params IS NULL` | 1,384 | Exactly equals core-only |
| `direction = 'A'` and `bgc_params IS NULL` | 1,384 | All core profiles are ascending |
| `direction = 'A'` and BGC | 626 | BGC ascending |
| `direction = 'D'` and BGC | 6 | BGC descending |
| `direction = 'D'` and core | 0 | No descending core profiles — correct |

The planner uses `JSON_LENGTH(COALESCE(p.bgc_params, '{}')) > 0` for `has_bgc=True` (robust against `{}` edge case) and `bgc_params IS NULL OR JSON_LENGTH(...) = 0` for `has_bgc=False`.

---

## 5. Acceptance Test Results

**Suite:** `backend/test_bgc_query_phase1.py`  
**Result: 20/20 passed** (live database: `argo_ocean_data_fresh`)

| # | Question | Expected | Result |
|---|---|---|---|
| P1 | "How many BGC profiles are available?" | `has_bgc=True`, `COUNT_PROFILES` | ✅ |
| P2 | "How many core-only profiles are available?" | `has_bgc=False`, `COUNT_PROFILES` | ✅ |
| P3 | "How many ARGO profiles are available?" | `has_bgc=None`, `COUNT_PROFILES` | ✅ |
| P4 | "Show profiles containing chlorophyll fluorescence." | `params=[CHLA_FLUORESCENCE]` | ✅ |
| P5 | "Show profiles containing chlorophyll measurements." | `params=[CHLA]` only | ✅ |
| P6 | "Which floats contain pH measurements?" | `float_level=True`, `PH_IN_SITU_TOTAL` | ✅ |
| P7 | "Which floats contain nitrate measurements?" | `float_level=True`, `NITRATE` | ✅ |
| P8 | "Show profiles containing dissolved oxygen." | `float_level=False` | ✅ |
| P9 | "Find ascending BGC profiles." | `has_bgc=True`, `direction=A` | ✅ |
| P10 | "Find descending core profiles." | `has_bgc=False`, `direction=D` | ✅ |
| DB1 | BGC profile count | 632 | ✅ |
| DB2 | Core profile count | 1,384 | ✅ |
| DB3 | Total profile count | 2,016 | ✅ |
| DB4 | DOXY profiles | EXECUTED, >0 rows | ✅ |
| DB5 | DOXY + CHLA profiles | EXECUTED, >0 rows | ✅ |
| DB6 | Floats with pH | `float_rows`, >0 WMOs | ✅ |
| DB7 | Floats with nitrate | `float_rows`, >0 WMOs | ✅ |
| DB8 | CHLA_FLUORESCENCE profiles | EXECUTED, >0 rows | ✅ |
| DB9 | Ascending BGC profiles | EXECUTED, all `direction='A'` | ✅ |
| DB10 | Descending core profiles | EXECUTED, 0 rows (valid) | ✅ |

---

## 6. Regression Test Results

**Suite:** `test_query_spec`, `test_query_parser`, `test_query_planner`, `test_query_pipeline`, `test_query_integration`, `test_query_router`, `test_query_boundary_4b2`, `test_query_reliability_4d`, `test_query_reliability_4e`, `test_query_remediation`

```
Ran 452 tests in 0.766s
OK (skipped=9)
```

Zero failures. Nine skipped tests require `INTELLIQUERY_LIVE_MYSQL=1` and are unrelated to Phase 1.

---

## 7. Before / After Examples

### Before Phase 1
```
Q: How many BGC profiles are available?
→ intent=CONVERSATIONAL (no BGC concept)
→ falls back to Qwen3 / ChromaDB semantic retrieval
→ answer is approximated or unavailable
```

### After Phase 1
```
Q: How many BGC profiles are available?
→ intent=COUNT_PROFILES  has_bgc=True
→ SQL: SELECT COUNT(*) AS profile_count FROM argo_profiles p
       WHERE JSON_LENGTH(COALESCE(p.bgc_params, '{}')) > 0
→ profile_count = 632
```

```
Q: Which floats contain pH measurements?
→ intent=PARAMETER_AVAILABILITY  float_level=True  params=[PH_IN_SITU_TOTAL]
→ SQL: SELECT DISTINCT f.wmo_number … WHERE JSON_CONTAINS_PATH(bgc_params, 'one', '$.PH_IN_SITU_TOTAL')
→ float_rows: [3902472, 3902473, …]
```

```
Q: Show profiles containing chlorophyll fluorescence.
→ params=[CHLA_FLUORESCENCE]  (not CHLA)
→ SQL: … WHERE JSON_CONTAINS_PATH(bgc_params, 'one', '$.CHLA_FLUORESCENCE') …
```

---

## 8. Limitations (Phase 1)

The following capabilities were intentionally excluded from Phase 1 and remain for Phase 2:

1. **ChromaDB metadata filtering** — BGC queries still use full-collection semantic search when routed through the ChromaDB path. Phase 2 will add `where={"has_bgc": True}` metadata filters.
2. **BGC scientific calculations** — depth profiles, oxygen saturation, etc. are not computed. `llmbackend.py` is unchanged.
3. **BGC parameter value queries** — "Find profiles where DOXY > 200 µmol/kg" is not yet supported (no value range parsing for BGC parameters).
4. **Mixed BGC+core comparisons** — questions comparing BGC and core statistics are not planned.
5. **CHLA_FLUORESCENCE data coverage** — the BGC dataset includes `CHLA_FLUORESCENCE`-labelled keys but coverage depends on individual float sensor fit-outs. The pipeline reports what is in the database; it does not assert coverage.

---

## 9. Recommended Phase 2 Work

| Priority | Work item |
|---|---|
| High | Metadata-filtered ChromaDB retrieval: pass `where={"has_bgc": True/False}` to ChromaDB when `spec.has_bgc` is set |
| High | BGC parameter value queries: extend `_profile_filters` for numeric range predicates on `bgc_params->>"$.DOXY"` etc. |
| Medium | Qwen3 context injection: include `has_bgc`, `direction`, `float_level` in the structured context passed to `llmbackend.py` for richer natural-language answers |
| Medium | Data Explorer BGC tab: surface BGC parameter availability per profile in the UI |
| Low | `CHLA` / `CHLA_FLUORESCENCE` disambiguation note in responses |

---

## 9. How to Verify

```powershell
cd "c:\AI Projects\IntelliQuery\backend"

# Acceptance tests (requires live MySQL)
python -m unittest test_bgc_query_phase1 -v

# Regression suite
python -m unittest test_query_spec test_query_parser test_query_planner `
  test_query_pipeline test_query_integration test_query_router `
  test_query_boundary_4b2 test_query_reliability_4d `
  test_query_reliability_4e test_query_remediation -v
```

---

## 10. Known Issues

None. All 20 acceptance tests and all 452 regression tests pass.
