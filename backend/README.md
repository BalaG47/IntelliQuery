# IntelliQuery — Backend Architecture & Database Documentation

## 1. Overview
IntelliQuery is an oceanographic data exploration application for ARGO float datasets. The system ingests NetCDF profile files, normalizes observations, tracks quality control (QC) flags, preserves calibration and provenance metadata, and serves data through a Flask backend and React frontend.

---

## 2. Database Schema & Index Architecture

The application relies on two primary relational tables in MySQL (`argo_ocean_data_fresh`): `argo_floats` and `argo_profiles`.

### 2.1 Existing Indexes

#### Table: `argo_floats`
- `PRIMARY KEY (float_id)`: Surrogate key for float entities.
- `UNIQUE KEY wmo_number (wmo_number)`: Enforces uniqueness for WMO float identifiers and powers WMO lookups.

#### Table: `argo_profiles`
- `PRIMARY KEY (profile_id)`: Surrogate key for individual profiles.
- `UNIQUE KEY float_id (float_id, cycle_number)`: Natural domain uniqueness constraint preventing duplicate cycles per float.
- `INDEX idx_profiles_source_hash (source_file_sha256, profile_index_in_file)`: Supports provenance auditing and deduplication checks during NetCDF ingestion.

---

## 3. Phase 2E — Index Evaluation & Performance Findings

During Phase 2E, an empirical index evaluation was conducted to determine whether dedicated secondary indexes on `argo_profiles` (`profile_time`, `(latitude, longitude)`, and `cycle_number`) were required to support Data Explorer, Map Points, and filtering queries.

### 3.1 The Role of the Composite `(float_id, cycle_number)` Index
In the application's query architecture (implemented in `database_manager.py`), user-facing queries joining `argo_floats` and `argo_profiles` do not execute full-table scans. Because `argo_floats` contains a small set of floats (22 floats) scanned via covering index `wmo_number`, MySQL executes nested-loop inner joins into `argo_profiles` using the composite `float_id (float_id, cycle_number)` index.

This restricts lookups to small per-float subsets (~63 profiles per float), applying spatial, temporal, and cycle filters in memory or as index-level post-filters.

### 3.2 Evaluation of Candidate Indexes

Benchmark tests were run across 30 iterations on an isolated database clone (`argo_idx_verify_test`) under identical query loads:

1. **`idx_profiles_time (profile_time)` — DEFERRED:**
   - Evaluated for default profile ordering (`ORDER BY profile_time DESC LIMIT 20`) and date range filters.
   - Benchmark: 5.88 ms baseline vs. 5.94 ms with index.
   - Finding: The optimizer continues to prefer the nested-loop join path through `float_id` rather than driving through `profile_time` (which would require random primary key lookups back into `argo_floats` for every candidate row). No measurable performance gain was observed.

2. **`idx_profiles_lat_lon (latitude, longitude)` / `(longitude, latitude)` — DEFERRED:**
   - Evaluated for spatial bounding-box counts and map-point coordinate retrieval.
   - Benchmark: 3.09 ms baseline vs. 3.30 ms (`lat, lon`) and 3.08 ms (`lon, lat`).
   - Finding: MySQL optimizer applies spatial bounds during the per-float scan; introducing a composite B-tree spatial index does not alter the execution plan and adds index write overhead without query acceleration.

3. **`idx_profiles_cycle (cycle_number)` — REJECTED:**
   - Evaluated for cycle-specific queries (e.g., `WHERE p.cycle_number = 9`).
   - Benchmark: 0.75 ms baseline vs. 0.65–0.66 ms with index.
   - Finding: The existing `(float_id, cycle_number)` unique index already resolves `cycle_number` lookups across the 22 floats in under 1 ms via indexed lookups (`float_id=f.float_id, cycle_number=9`). A standalone single-column index on `cycle_number` is redundant.

### 3.3 Summary of Benchmark Results

| Query Pattern | Baseline (No New Index) | Time Only | Lat/Lon Only | Lon/Lat Only | All 3 Proposed | Plan Impact |
|---|---|---|---|---|---|---|
| **Default List (`ORDER BY profile_time DESC LIMIT 20`)** | 5.88 ms | 5.94 ms | 5.69 ms | 5.93 ms | 5.77 ms | Nested loop via `float_id` + filesort unchanged |
| **Date Range Filter** | 4.76 ms | 4.89 ms | 4.47 ms | 4.96 ms | 4.45 ms | Filter applied during per-float lookup |
| **Spatial Bounding Box (`COUNT(*)`)** | 3.09 ms | 3.02 ms | 3.30 ms | 3.08 ms | 2.95 ms | Filter applied during per-float lookup |
| **Map Points (`LIMIT 5001`)** | 5.27 ms | 5.27 ms | 5.44 ms | 5.42 ms | 4.91 ms | Nested loop via `float_id` + filesort unchanged |
| **Cycle Number (`cycle_number = 9`)** | 0.75 ms | 0.65 ms | 0.69 ms | 0.75 ms | 0.66 ms | Handled by existing composite `float_id` key |

### 3.4 Conditions for Re-evaluation
Index evaluation should be repeated if:
- The dataset grows significantly beyond academic scale (e.g., >50,000 profiles or >500 floats).
- New endpoints are introduced that query `argo_profiles` directly without joining `argo_floats`.
- Full-table date range or spatial export queries demonstrate filesort or table scan bottlenecks exceeding acceptable interactive latency thresholds (>100 ms).

---

## 4. Final Architectural Decision

**Migration V3 is skipped.** The current index configuration (`PRIMARY`, `float_id (float_id, cycle_number)`, and `idx_profiles_source_hash`) adequately and efficiently supports the application's existing query patterns, with all core queries completing in under 6 ms. No database DDL or index additions are required at this stage.
