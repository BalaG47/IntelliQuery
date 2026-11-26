# IntelliQuery — Core ↔ Synthetic-BGC Product Matching Investigation

**Question:** can core (`D`/`R`) and synthetic BGC (`SD`/`SR`) profiles from the
downloaded corpus be associated reliably enough to store one logical profile
without losing measurements or duplicating profiles?

**Date:** 2026-11-23
**Dataset:** `backend/bgc data/bgc raw data/incois/<WMO>/profiles/*.nc` (read-only)
**Method:** `backend/scripts/bgc_product_matching.py` (metadata-only scan of all
1,264 files + vertical-grid comparison of all 632 BGC profiles).
**Changes made:** analysis script and this document only. No migration, no
ingestion code, no database, no dataset change.

> **Status: implemented (Option D).** The recommendation below was implemented as
> `backend/bgc_matching.py` (matching rules), `backend/migrations/
> V4__logical_profile_identity_and_bgc_axis.sql`, group-level ingestion in
> `backend/data_processor.py`, and the verifier
> `backend/scripts/bgc_controlled_ingestion.py`. Sections 8–9 describe the design
> *before* implementation; the implemented column set is `direction`,
> `bgc_pressure`, `bgc_source_metadata` plus
> `UNIQUE (float_id, cycle_number, direction, profile_time)`, and the discarded
> variants are recorded in `source_metadata.core_variants`. Only the controlled
> sample was ingested; the corpus was not.

---

## 1. Representative file pairs examined

Six `(float, cycle)` groups covering every structural case in the corpus.

| # | Core file(s) | BGC file(s) | Structure |
|---|---|---|---|
| 1 | `D2902306_001` | `SD2902306_001` | 1 core record ↔ 1 BGC record |
| 2 | `D2902306_002` (2 N_PROF) | `SD2902306_002` | 2 core records ↔ 1 BGC record |
| 3 | `R2903951_001` (4 N_PROF) | `SR2903951_001` | 4 core records ↔ 1 BGC record |
| 4 | `D3902581_001` (4) + `D3902581_001D` (3) | `SD3902581_001` + `SD3902581_001D` | ascending **and** descending profile in one cycle; 7 core records ↔ 2 BGC records; BGC descending profile has no position |
| 5 | `D7902170_001` (2) | `SD7902170_001` | 2 core records ↔ 1 BGC record |
| 6 | `D3902581_006` (4) + `D3902581_006D` (3) | `SD3902581_006` + `SD3902581_006D` | second ascending/descending cycle |

Corpus-wide structure:

| Property | Count |
|---|---|
| Files | 1,264 (`D` 246, `R` 386, `SD` 246, `SR` 386) |
| Profile records (`N_PROF` summed) | 2,509 (core 1,877, BGC 632) |
| `(float, cycle)` groups | **626** — every one contains **both** products |
| Core files | 13 × 1 N_PROF, 303 × 2, 6 × 3, 310 × 4 |
| BGC files | 632 × 1 N_PROF (no multi-profile BGC file) |
| Groups with two BGC profiles | 6 (float 3902581, cycles 1–6: ascending + descending) |

## 2. Profile-level metadata comparison

Column meanings: `idx` = `N_PROF` position, `dir` = `DIRECTION`, `mode` =
`DATA_MODE`, `lvl` = number of valid `PRES` levels, `BGC` = BGC parameters in
`STATION_PARAMETERS`.

| File | idx | cycle | JULD | lat | lon | dir | mode | lvl | BGC |
|---|---|---|---|---|---|---|---|---|---|
| `D2902306_001` | 0 | 1 | 27158.51497 | 17.9667 | 68.0348 | A | D | 996 | – |
| `SD2902306_001` | 0 | 1 | 27158.51497 | 17.9667 | 68.0348 | A | – | 996 | PH |
| `D2902306_002` | 0 | 2 | 27168.38251 | 18.0373 | 67.9196 | A | D | 998 | – |
| `D2902306_002` | 1 | 2 | 27168.38251 | 18.0373 | 67.9196 | A | A | 72 | – |
| `SD2902306_002` | 0 | 2 | 27168.38251 | 18.0373 | 67.9196 | A | – | 1035 | PH,DOXY,CHLA,BBP700 |
| `R2903951_001` | 0–3 | 1 | 27394.57014 | −4.0065 | 76.2973 | A | R | 85 / 2 / 87 / 87 | – |
| `SR2903951_001` | 0 | 1 | 27394.57014 | −4.0065 | 76.2973 | A | – | 174 | DOXY,CHLA,CHLA_FLUOR,BBP700 |
| `D3902581_001` | 0–3 | 1 | 27040.56319 | 1.0985 | 80.4416 | A | D / R / R / R | 243 / 2 / 476 / 494 | – |
| `SD3902581_001` | 0 | 1 | 27040.56319 | 1.0985 | 80.4416 | A | – | 732 | DOXY,CHLA,CHLA_FLUOR,BBP700 |
| `D3902581_001D` | 0–2 | 1 | 27039.15347 | – | – | D | D / R / R | 224 / 1769 / 1775 | – |
| `SD3902581_001D` | 0 | 1 | 27039.15347 | – | – | D | – | 1857 | DOXY,CHLA,CHLA_FLUOR,BBP700 |
| `D7902170_001` | 0–1 | 1 | 27139.88144 | 0.1514 | 82.8574 | A | D / A | 997 / 75 | – |
| `SD7902170_001` | 0 | 1 | 27139.88144 | 0.1514 | 82.8574 | A | – | 1038 | PH,DOXY,CHLA,BBP700 |
| `D3902581_006` | 0–3 | 6 | 27072.56250 | 1.0126 | 82.1156 | A | D / R / R / R | 243 / 2 / 480 / 495 | – |
| `SD3902581_006` | 0 | 6 | 27072.56250 | 1.0126 | 82.1156 | A | – | 735 | DOXY,CHLA,CHLA_FLUOR,BBP700 |
| `D3902581_006D` | 0–2 | 6 | 27071.61389 | 1.0118 | 82.0768 | D | D / R / R | 223 / 1755 / 1755 | – |
| `SD3902581_006D` | 0 | 6 | 27071.61389 | 1.0118 | 82.0768 | D | – | 1888 | DOXY,CHLA,CHLA_FLUOR,BBP700 |

Observations:

* **Index is not identity.** A core file's 2–4 `N_PROF` entries are *copies of
  the same physical profile* (identical `JULD`, position and `DIRECTION`) that
  differ in `DATA_MODE` and in how many levels carry data (243 / 2 / 476 / 494).
  The single BGC record is a sibling of all of them.
* **BGC files never have more than one `N_PROF`.** Every `N_PROF` in a core file
  is therefore *not* mirror-image paired with a BGC `N_PROF`.
* **Direction matters.** Float 3902581 cycles 1–6 each contain an ascending and a
  descending profile; they share `CYCLE_NUMBER` and `profile_index_in_file = 0`.
  Only `JULD` and `DIRECTION` separate them. The descending core file
  (`…001D`) is 4× larger and the BGC `…001D` has no position fix.
* **No profile identifier exists.** Neither product carries a profile-id
  variable or attribute (`REFERENCE_DATE_TIME`/`DC_REFERENCE` are not profile
  keys), so association has to be derived from metadata.
* **Provenance of the BGC product.** `SD`/`SR` files were produced 2026-07-09 by
  `software_version 1.22 ("… for ARGO_simplified_profile")`, `FORMAT_VERSION`
  1.0, i.e. they were *generated from the core profiles*.

## 3. Evidence for matching or non-matching profiles

Corpus-wide, grouping by `(float, cycle)` and comparing the sets of
`(JULD, DIRECTION)` signatures:

| Result | Groups |
|---|---|
| Core and BGC signature sets **identical** | **626 / 626** |
| BGC subset / core subset / partial overlap | 0 / 0 / 0 |
| Same `JULD`, different `DIRECTION` | 0 |
| Different `JULD` | 0 |

Position never contradicts the association: across 1,877 BGC-record ↔
core-record comparisons, 1,874 positions are equal to <1e-6°, 3 are both
missing (the `SD3902581_001D` profile against the three records of
`D3902581_001D`), and **0** conflict.

Vertical grids and values (best core candidate per BGC profile):

| Check | Result |
|---|---|
| BGC profiles analysed | 632 |
| BGC profiles with no usable core candidate | **0** |
| Core grid fully contained in the BGC grid | **619** (97.9 %) |
| BGC `PRES` rescaled by ≈1000× (grid unusable) | **13** (2.1 %) |
| Identical grids | 0 (the BGC grid always has extra levels) |
| Core levels also present in BGC | 332,475 |
| `TEMP` equal at those levels | **332,473** (99.999 %) |
| `PSAL` equal at those levels | **332,470** (99.999 %) |
| Core candidate with most levels = best match | 316 / 632 |

The 13 rescaled files (`PRES` in the 0.006–2.0 range instead of 6–2000 dbar):
`SD2902306_001, _011, _016, _017, _018, _023, _024, _025, _026`,
`SR2902306_039`, `SR7902170_022`, `SR7902170_026`, `SR7902200_015`.
This is a defect of the synthetic product; the BGC values themselves are not
affected, only the pressure axis needed to interpret them.

Conclusion: the products describe the **same physical profiles**, and the BGC
product is a **superset resampling** of the core profile — every core level
appears in the BGC grid with (essentially) identical `TEMP`/`PSAL`. The
duplicate `N_PROF` entries inside core files are *processing variants* of one
profile, not distinct profiles.

## 4. Unambiguous and ambiguous matches

| Level | Unambiguous | Ambiguous | Unmatched |
|---|---|---|---|
| BGC profile → logical profile `(float, cycle, JULD, direction)` | **626 / 626** | 0 | 0 |
| BGC profile → *which core record* to use | 13 | **619** (2–4 core variants) | 0 |
| BGC profile → usable pressure axis | 619 | 13 (rescaled) | 0 |

Core variants per physical profile: 1 → 13 profiles, 2 → 303, 3 → 6, 4 → 310.
The 619 "ambiguous" cases are not identity ambiguity: all candidates share the
same `JULD`, `DIRECTION` and position, so they are the same profile; the choice
of which variant's arrays to keep is a **documented preference**, not a guess.

Variant choice matters because the copies are not equivalent:

| Data-mode sets per physical profile | Profiles |
|---|---|
| `('A',)` | 179 |
| `('A','D')` | 124 |
| `('D','R')` | 113 |
| `('A','R')` | 108 |
| `('R',)` | 99 |
| `('D',)` | 9 |

246 profiles contain a delayed-mode record. Of those, the delayed record holds
100 % of the maximum level count in 133 cases, 90–99 % in 100, 50–89 % in 1 and
less than 50 % in 12 (median ratio 1.000). A "prefer delayed when it has ≥90 %
of the maximum levels, else take the most levels" rule keeps adjusted data for
233/246 profiles and still maximises level coverage.

## 5. Evaluation of the ingestion options

| | A — BGC primary | B — core primary + attach | C — separate rows | D — merge with verified match |
|---|---|---|---|---|
| Data loss | none | none | none | none if fallback exists |
| Uses official core product | **no** | yes | yes | yes |
| Profile counts stay meaningful | yes (632 rows) | yes (626 rows) | **no** (1,258 rows → 2× counting) | yes (626 rows) |
| BGC pressure axis | consistent (own grid) | needs a separate axis | consistent (own row) | needs a separate axis only when grids differ |
| Handles the 13 rescaled `PRES` files | **no** (would store a 1000× wrong core `PRES`) | yes (core `PRES` kept) | yes | yes (flag it) |
| Query layer impact | small | small | **large** (every count/filter must dedupe by product) | small |
| Integrity risk | mis-scaled core `PRES`, no `DATA_MODE` | none if matching is verified | none | none if the grid check is enforced |
| Complexity | low | medium | medium | medium |

Option A is rejected: 13 BGC files would inject a pressure axis that is wrong by
three orders of magnitude, and the BGC product carries no `DATA_MODE`.
Option C is rejected for now: it doubles the row count and forces every existing
Data Explorer / query aggregate to add a product filter to stay correct, for no
benefit in this corpus where the products always co-occur.
Option B and Option D converge; D adds the explicit fallback the brief requires.

## 6. Recommended ingestion architecture — Option D, scoped

1. **Group before inserting.** Build the corpus index (`float`, `cycle`,
   `JULD`, `direction`, product, file, index) and process one `(float, cycle)`
   group at a time instead of one file at a time.
2. **Match by `(float, cycle, JULD, direction)`**, never by `N_PROF` index.
   In this corpus `JULD` alone already separates every profile; `direction` is
   included as the semantic guard and because it is what distinguishes the
   `…001`/`…001D` pairs.
3. **One row per logical profile.** From the core side, pick the delayed-mode
   record if it has ≥90 % of the maximum level count, otherwise the record with
   the most levels; list the other variants in `source_metadata.core_variants`
   (`file`, `profile_index_in_file`, `data_mode`, `levels`). The source files
   remain the archive, so nothing is unrecoverable.
4. **Attach BGC arrays with their own axis.** Store `bgc_params`, `bgc_qc`,
   `bgc_adjusted`, `bgc_adjusted_error`, `bgc_adjusted_qc`, and the BGC product's
   own `bgc_pressure` so the arrays stay interpretable when the grids differ.
   Never interpolate or index-align across products.
5. **Validate before attaching.** Require the BGC `PRES` range to overlap the
   core profile's range. Attach with `bgc_grid_check = 'ok'` when it does;
   attach with `bgc_grid_check = 'pres_scale_suspect'` for the 13 rescaled
   files and exclude them from BGC-vs-depth analysis until the source is fixed.
   If the check fails for another reason, or no core record exists, insert the
   BGC profile as its own explicitly typed row (`product_type='bgc'`) rather than
   guessing — the brief's "retain a separately identified profile" case.
6. **Idempotency** follows from the new unique key; re-ingesting a group updates
   nothing and reports `skipped`.

## 7. Recommended logical profile identity

**Logical profile:** `(float_id (WMO), cycle_number, direction, profile_time)`

* Unique in the corpus: 626 logical profiles across 2,509 records, 0 collisions.
* `profile_time` is the Argo `JULD` (already stored as `profile_time`, second
  precision is sufficient — profiles within a cycle are hours to days apart).
* `direction` is `A`/`D`; it is what separates the ascending/descending pairs.
* `profile_index_in_file` is **provenance only** — it does not identify a
  profile, and it collides across products for the `…D` files.

**Storage key:** `UNIQUE (float_id, cycle_number, direction, profile_time)`.

## 8. Required schema changes (proposal only — not applied)

| Change | Purpose |
|---|---|
| `direction CHAR(1) NOT NULL DEFAULT '-'` | part of the logical identity; `'-'` for legacy rows |
| `bgc_pressure JSON NULL` | the BGC product's own pressure axis |
| `bgc_source_metadata JSON NULL` | BGC file, `profile_index_in_file`, product type, grid check |
| Replace `uq_argo_profiles_identity (float_id, cycle_number, profile_index_in_file)` with `uq_argo_profiles_logical (float_id, cycle_number, direction, profile_time)` | one row per logical profile |
| Keep `profile_index_in_file`, add `source_metadata.core_variants` | provenance of the discarded processing variants |

No other column is needed: `bgc_params IS NOT NULL` already marks BGC-bearing
rows, and `source_metadata` already carries product type.

## 9. Proposed implementation plan

1. **Migration V4** (new file, reviewed on the disposable database first):
   nullable `direction`/`bgc_pressure`/`bgc_source_metadata`, the new unique key,
   and a backfill of `direction` for existing rows where the source file is still
   available (legacy rows without a recoverable direction keep `'-'`).
2. **Group-level ingestion** in `data_processor`: index the corpus metadata,
   group by `(float, cycle)`, choose the core variant, match the BGC record,
   validate the grid, write one row. Keep `process_file` for single-file uploads
   (it becomes "a group of one").
3. **Tests:** synthetic fixtures for grouping/matching/variant choice/grid
   validation; real-file tests asserting one row per logical profile for
   `3902581/1` and `3902581/6` (ascending + descending), the rescaled-`PRES`
   flag on `SD2902306_001`, and idempotent re-ingestion.
4. **Controlled validation** on the 10-file sample plus the `3902581` pair:
   expect 13 rows from 13 logical profiles (not 15 records), no duplicates,
   `bgc_pressure` lengths equal to the BGC arrays.
5. Only then consider the 50–100 profile run. Full ingestion remains out of
   scope.

## 10. Risks and limitations

1. **Variant choice is a judgement.** Keeping one core variant per profile drops
   the other 1–3 processing variants from the database (they stay in the corpus
   and are listed in `source_metadata`). Adjusted-vs-complete trade-offs exist
   (12 profiles have a delayed record below 50 % of the maximum level count).
2. **The 13 rescaled `PRES` files** are a defect of the synthetic product. They
   can be stored honestly (flag + own axis) but cannot be used for BGC-vs-depth
   work without a source fix; the correct scaling is not applied because that
   would be an unverified correction.
3. **Second-precision `profile_time`.** `JULD` is a float day; keying on the
   stored `DATETIME` rounds to the second. No collisions occur in this corpus,
   but a future float sampling twice within one second would need sub-second
   precision.
4. **No profile identifier exists**, so association remains derived metadata,
   not an authoritative identifier. A future corpus with real `BD`/`BR` products
   and different `N_PROF` conventions must re-run this analysis.
5. **Query behaviour changes**: a cycle may now legitimately have two rows
   (ascending + descending, 6 cycles). Any test or query that assumed one row
   per `(float, cycle)` needs updating.
6. **ChromaDB** documents are built per row; the merged row should mention BGC
   parameters and direction.
7. Not investigated: cross-DAC behaviour (one DAC present), real BGC products
   (none present), and the scientific validity of the synthetic BGC values
   themselves.
