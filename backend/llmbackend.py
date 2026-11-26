import json
import logging
import math
import re

import ollama
import streamlit as st
from sqlalchemy import text

from bgc_calculations import (
    BGC_PARAMETER_UNITS,
    calculate_bgc_statistics,
)
from database_manager import DatabaseManager
from query_integration import (
    DeterministicPipelineError,
    answer_deterministically,
    has_structured_refinement,
)
from query_parser import parse_query
from query_router import ArgoQueryRouter, Route
from query_spec import BGC_PARAMETERS, Parameter

# Canonical token set used for exact parameter membership checks against the
# comma-separated ``bgc_parameters`` ChromaDB metadata field.
_BGC_PARAM_TOKENS: frozenset[str] = frozenset(p.value for p in BGC_PARAMETERS)


class ChatRetrievalError(RuntimeError):
    """A retrieval failure, as distinct from a successful empty result.

    Raised when MySQL or ChromaDB cannot be queried. Callers must not
    translate this into a zero count or an empty list, because "the query
    failed" and "the query matched nothing" mean different things to a
    scientist.
    """


class ArgoRAG:
    """
    Retrieval-Augmented Generation backend for ARGO oceanographic data.

    Architecture:
        User question
            ↓
        WMO detection / semantic retrieval
            ↓
        MySQL data retrieval
            ↓
        Python numerical processing
            ↓
        Compact factual context
            ↓
        Qwen3 8B for natural-language explanation

    Important:
        Numerical data is retrieved and processed by Python.
        The LLM is not treated as the source of truth for measurements.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self._check_ollama_setup()

    # ============================================================
    # OLLAMA
    # ============================================================

    def _check_ollama_setup(self):
        """Check that Ollama is running and Qwen3 is available."""

        try:
            model_list = ollama.list().models
            models = [m.model for m in model_list if m.model]

            if not any("qwen3:8b" in model for model in models):
                logging.warning(
                    "Qwen3 8B not found. Run: ollama pull qwen3:8b"
                )

        except Exception as e:
            logging.error(f"Could not connect to Ollama: {e}")
            st.error(
                "Could not connect to Ollama. "
                "Please make sure Ollama is running."
            )

    # ============================================================
    # JSON / DATA HELPERS
    # ============================================================

    @staticmethod
    def _parse_array(value):
        """
        Safely convert a database JSON array into a Python list.
        """

        if value is None:
            return []

        if isinstance(value, list):
            return value

        if isinstance(value, tuple):
            return list(value)

        if isinstance(value, str):

            try:
                parsed = json.loads(value)

                if isinstance(parsed, list):
                    return parsed

            except (json.JSONDecodeError, TypeError):
                pass

        return []

    @staticmethod
    def _round_number(value, digits=3):
        """Round numerical values while preserving useful precision."""

        if value is None:
            return None

        try:
            return round(float(value), digits)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _fmt_value(value):
        """Render a value for prose without turning missing data into zero."""

        return "unavailable" if value is None else value

    @staticmethod
    def _valid_numbers(values):
        """
        Return only finite numeric observations from a measurement array.

        Missing (``None``), non-numeric, NaN and infinite values are skipped.
        Nothing is coerced to zero, and the input array is not modified.
        """

        numbers = []

        if not values:
            return numbers

        for value in values:
            if value is None or isinstance(value, bool):
                continue

            try:
                number = float(value)
            except (TypeError, ValueError):
                continue

            if not math.isfinite(number):
                continue

            numbers.append(number)

        return numbers

    # ============================================================
    # QUESTION DETECTION
    # ============================================================

    @staticmethod
    def _contains_profile_request(question):
        """
        Detect questions that primarily request an actual
        temperature/salinity/pressure profile.
        """

        q = question.lower()

        profile_words = [
            "temperature profile",
            "salinity profile",
            "pressure profile",
            "temperature measurements",
            "salinity measurements",
            "temperature values",
            "salinity values",
        ]

        return any(word in q for word in profile_words)

    @staticmethod
    def _requested_variable(question):
        """
        Determine which profile variable the user is asking for.
        """

        q = question.lower()

        if "temperature" in q:
            return "temperature"

        if "salinity" in q:
            return "salinity"

        if "pressure" in q:
            return "pressure"

        return None

    @staticmethod
    def _is_profile_list_request(question):
        """Return whether the user explicitly wants profile records listed."""

        return bool(
            re.search(
                r"\b(list|show|display|give)\b.*\bprofiles?\b",
                question.lower(),
            )
        )

    @staticmethod
    def _requested_profile_limit(question):
        """Read an explicit 'first/top N' or 'latest N' profile request."""

        match = re.search(
            r"\b(?:(?:first|top)\s+(\d+)\s+profiles?"
            r"|(?:latest|newest|most recent)\s+(\d+)\s+profiles?"
            r"|(\d+)\s+(?:latest|newest|most recent)\s+profiles?"
            r"|(\d+)\s+profiles?)\b",
            question.lower(),
        )

        if not match:
            return None

        return int(next(group for group in match.groups() if group))

    # ============================================================
    # MYSQL RETRIEVAL
    # ============================================================

    @staticmethod
    def _is_float_enumeration_request(question):
        """
        Detect questions that ask for the set/count of available
        ARGO floats rather than measurements from a particular profile.

        These questions must bypass semantic retrieval: ChromaDB returns
        the most similar profiles, not a complete enumeration of floats.
        """

        q = question.lower().strip()

        enumeration_phrases = [
            "list of available float ids",
            "list available float ids",
            "available float ids",
            "available float id",
            "list of float ids",
            "list float ids",
            "list all float ids",
            "show all float ids",
            "show me all float ids",
            "list of available floats",
            "list available floats",
            "available floats",
            "which floats are available",
            "what floats are available",
            "which float ids are available",
            "what float ids are available",
            "which wmo numbers are available",
            "what wmo numbers are available",
            "list wmo numbers",
            "list all wmo numbers",
            "available wmo numbers",
            "how many floats are available",
            "number of available floats",
            "how many float ids",
        ]

        return any(phrase in q for phrase in enumeration_phrases)

    @staticmethod
    def _is_latest_request(question):
        """Detect a request for the most recent profile(s)."""

        return bool(
            re.search(
                r"\b(latest|most recent|newest)\b",
                question.lower()
            )
        )

    def _build_latest_profile_answer(
        self, wmo_number, total_count, context_data
    ):
        """Describe the newest retrieved profile(s) without LLM inference."""

        if len(context_data) == 1:
            lines = [
                f"WMO {wmo_number} has {total_count} profiles. "
                "The most recent one is:"
            ]
        else:
            lines = [
                f"WMO {wmo_number} has {total_count} profiles. "
                f"The {len(context_data)} most recent are shown newest first:"
            ]

        for profile in context_data:
            summary = self._profile_summary(profile)
            lines.append(
                f"Cycle {summary.get('cycle_number', 'N/A')} · "
                f"{summary.get('profile_time', 'N/A')} · "
                f"{summary.get('latitude', 'N/A')}°N, "
                f"{summary.get('longitude', 'N/A')}°E"
            )

        if len(context_data) == 1:
            summary = self._profile_summary(context_data[0])
            for name, label, unit in (
                ("temperature", "Temperature", "°C"),
                ("salinity", "Salinity", "PSU"),
                ("pressure", "Pressure", "dbar"),
            ):
                valid = summary.get(f"{name}_valid_count", 0)
                if valid:
                    lines.append(
                        f"{label}: {summary[f'{name}_min']}–"
                        f"{summary[f'{name}_max']} {unit} "
                        f"(mean {summary[f'{name}_mean']} {unit}, "
                        f"{valid} valid levels)"
                    )

        return "\n".join(lines)

    @staticmethod
    def _is_profile_count_request(question):
        """Detect requests asking for profile counts per float/WMO."""
        q = question.lower().strip()

        count_phrases = [
            "how many profiles are available for each float",
            "how many profiles are available for each float id",
            "how many profiles does each float have",
            "number of profiles for each float",
            "profile count for each float",
            "profile counts for each float",
            "how many profiles for each wmo",
            "how many profiles are there for each wmo",
            "profiles available for each wmo",
            "count of profiles for each wmo",
        ]

        return any(phrase in q for phrase in count_phrases)

    def _retrieve_profile_counts_by_wmo(self):
        """Return the exact number of profiles registered for every WMO."""
        query = text(
            """
            SELECT
                f.wmo_number,
                COUNT(p.profile_id) AS profile_count
            FROM argo_floats f
            LEFT JOIN argo_profiles p
                ON p.float_id = f.float_id
            GROUP BY f.wmo_number
            ORDER BY f.wmo_number
            """
        )

        try:
            with self.db_manager.mysql_engine.connect() as conn:
                results = conn.execute(query).fetchall()
                return [
                    {
                        "wmo_number": int(row[0]),
                        "profile_count": int(row[1]),
                    }
                    for row in results
                    if row[0] is not None
                ]
        except Exception as e:
            logging.error(
                f"Error retrieving profile counts by WMO: {e}"
            )
            raise ChatRetrievalError(
                "Profile counts could not be retrieved from the database."
            ) from e

    @staticmethod
    def _build_profile_counts_answer(profile_counts):
        """Build a deterministic answer for profile counts by WMO."""
        if not profile_counts:
            return (
                "No ARGO profile counts are currently available "
                "in the connected database."
            )

        lines = [
            "Profile counts by WMO number:"
        ]

        for item in profile_counts:
            lines.append(
                f"WMO {item['wmo_number']}: "
                f"{item['profile_count']} profiles"
            )

        total = sum(
            item["profile_count"]
            for item in profile_counts
        )

        lines.append(
            f"Total: {total} profiles across "
            f"{len(profile_counts)} float IDs."
        )

        return "\n".join(lines)

    def _retrieve_available_wmos(self):
        """
        Retrieve the complete set of WMO numbers from MySQL.

        This is deliberately a relational query rather than semantic
        retrieval because an enumeration question requires all matching
        records, not the top-k semantically similar profiles.
        """

        query = text(
            """
            SELECT DISTINCT wmo_number
            FROM argo_floats
            ORDER BY wmo_number
            """
        )

        try:
            with self.db_manager.mysql_engine.connect() as conn:
                results = conn.execute(query).fetchall()

                return [
                    int(row[0])
                    for row in results
                    if row[0] is not None
                ]

        except Exception as e:
            logging.error(
                f"Error retrieving available WMO numbers: {e}"
            )
            raise ChatRetrievalError(
                "The available float list could not be retrieved from the database."
            ) from e

    def _build_float_enumeration_answer(self, wmo_numbers):
        """Build a concise deterministic answer for float enumeration."""

        if not wmo_numbers:
            return (
                "No ARGO float IDs are currently available in the "
                "connected database."
            )

        ids = ", ".join(
            str(wmo)
            for wmo in wmo_numbers
        )

        return (
            f"There are {len(wmo_numbers)} available ARGO float IDs "
            f"in the connected database: {ids}."
        )

    def _count_profiles_by_wmo(self, wmo_number):
        """Return the authoritative number of profiles for one WMO."""

        query = text(
            """
            SELECT COUNT(*)
            FROM argo_profiles p
            JOIN argo_floats f ON p.float_id = f.float_id
            WHERE f.wmo_number = :wmo_number
            """
        )

        try:
            with self.db_manager.mysql_engine.connect() as conn:
                return int(conn.execute(
                    query, {"wmo_number": wmo_number}
                ).scalar_one())
        except Exception as e:
            logging.error(f"Error counting profiles for WMO {wmo_number}: {e}")
            raise ChatRetrievalError(
                f"The profile count for WMO {wmo_number} could not be retrieved."
            ) from e

    def _retrieve_by_wmo(self, wmo_number, limit, newest_first=False):
        """
        Retrieve profiles for a specific WMO.

        ``limit`` is intentionally explicit. General questions use a small
        context window, while an explicit list request can request all
        profiles or its own first-N window.

        ``newest_first`` switches the ordering in the SQL itself so that a
        "latest" question receives the most recent records. Rows with a
        NULL timestamp always sort last (`(p.profile_time IS NULL)` sorts 0
        before 1 in MySQL), so a missing date is never treated as the newest
        observation.
        """

        context_data = []

        # cycle_number is unique per float, so it makes equal timestamps
        # deterministic without changing the timestamp ordering itself.
        order_clause = (
            "ORDER BY (p.profile_time IS NULL), p.profile_time DESC, "
            "p.cycle_number DESC"
            if newest_first
            else "ORDER BY (p.profile_time IS NULL), p.profile_time ASC, "
            "p.cycle_number ASC"
        )

        # Only the fixed ordering literal above is interpolated; every
        # user-supplied value remains a bound parameter.
        query = text(
            f"""
            SELECT
                p.profile_id,
                p.cycle_number,
                p.profile_time,
                p.latitude,
                p.longitude,
                p.pressure,
                p.temperature,
                p.salinity,
                p.bgc_params,
                f.wmo_number
            FROM argo_profiles p
            JOIN argo_floats f
                ON p.float_id = f.float_id
            WHERE f.wmo_number = :wmo_number
            {order_clause}
            LIMIT :limit
            """
        )

        try:
            with self.db_manager.mysql_engine.connect() as conn:

                results = conn.execute(
                    query,
                    {"wmo_number": wmo_number, "limit": limit}
                ).fetchall()

                for row in results:
                    context_data.append(
                        dict(row._mapping)
                    )

        except Exception as e:
            logging.error(
                f"Error retrieving WMO {wmo_number}: {e}"
            )
            raise ChatRetrievalError(
                f"Profiles for WMO {wmo_number} could not be retrieved."
            ) from e

        return context_data

    def _retrieve_arabian_sea_trend(self, variable, start_year, end_year):
        """Calculate annual measurement means in the approximate Arabian Sea."""

        column = "salinity" if variable == "salinity" else "temperature"
        query = text(
            f"""
            SELECT YEAR(profile_time) AS profile_year, {column} AS measurements
            FROM argo_profiles
            WHERE YEAR(profile_time) BETWEEN :start_year AND :end_year
              AND latitude BETWEEN 0 AND 30
              AND longitude BETWEEN 45 AND 78
            ORDER BY profile_time
            """
        )
        values_by_year = {year: [] for year in range(start_year, end_year + 1)}

        try:
            with self.db_manager.mysql_engine.connect() as conn:
                rows = conn.execute(
                    query,
                    {"start_year": start_year, "end_year": end_year},
                ).fetchall()
        except Exception as e:
            logging.error(f"Error retrieving Arabian Sea trend: {e}")
            raise ChatRetrievalError(
                "The regional trend could not be retrieved from the database."
            ) from e

        for row in rows:
            for value in self._parse_array(row.measurements):
                try:
                    numeric_value = float(value)
                    if math.isfinite(numeric_value):
                        values_by_year[int(row.profile_year)].append(numeric_value)
                except (TypeError, ValueError):
                    continue

        return {year: values for year, values in values_by_year.items() if values}

    @staticmethod
    def _parse_arabian_sea_trend_request(question):
        """Return (variable, start year, end year) for a supported trend query."""

        normalized = question.lower()
        years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", normalized)]
        if len(years) < 2:
            return None
        variable = "salinity" if "salinity" in normalized else "temperature"
        return variable, min(years), max(years)

    def _build_arabian_sea_trend_answer(
        self, variable, start_year, end_year, values_by_year
    ):
        """Build a factual trend answer from MySQL measurements only."""

        if not values_by_year:
            return (
                f"No {variable} measurements were found in the approximate "
                f"Arabian Sea bounds for {start_year}–{end_year}."
            )

        annual_means = {
            year: sum(values) / len(values)
            for year, values in values_by_year.items()
        }
        lines = [
            f"Mean {variable} by year for the approximate Arabian Sea "
            f"(0–30°N, 45–78°E):"
        ]
        for year in range(start_year, end_year + 1):
            if year in annual_means:
                lines.append(
                    f"{year}: {annual_means[year]:.3f} "
                    f"from {len(values_by_year[year])} measurements"
                )
            else:
                lines.append(f"{year}: no measurements found")

        observed_years = sorted(annual_means)
        if len(observed_years) >= 2:
            change = annual_means[observed_years[-1]] - annual_means[observed_years[0]]
            direction = "increased" if change > 0 else "decreased" if change < 0 else "did not change"
            lines.append(
                f"Across the available endpoints ({observed_years[0]}–{observed_years[-1]}), "
                f"the annual mean {direction} by {abs(change):.3f}."
            )
        else:
            lines.append("Only one year has measurements, so a trend cannot be calculated.")

        return "\n".join(lines)

    @staticmethod
    def _build_profile_list_answer(wmo_number, total_count, displayed_count):
        """Describe exactly what was returned, without an LLM inference."""

        if displayed_count == total_count:
            return (
                f"WMO {wmo_number} has {total_count} profiles. "
                "All of them are listed below in chronological order."
            )

        return (
            f"WMO {wmo_number} has {total_count} profiles in total. "
            f"The first {displayed_count} are listed below in chronological order."
        )

    # ------------------------------------------------------------------
    # BGC-aware semantic retrieval helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bgc_where_filter(parsed) -> dict | None:
        """Return a ChromaDB ``where`` clause derived from the parsed query.

        Returns ``None`` when no BGC product restriction is requested so that
        the existing unrestricted retrieval behaviour is preserved exactly.
        Only an *explicit* user request for BGC or core-only profiles triggers
        a filter — ocean-science content alone does not.
        """
        try:
            spec = parsed.validation.spec if parsed.validation else None
            if spec is None:
                return None
            if spec.has_bgc is True:
                return {"has_bgc": True}
            if spec.has_bgc is False:
                return {"has_bgc": False}
        except Exception:
            pass
        return None

    @staticmethod
    def _requested_bgc_params(parsed) -> frozenset[str]:
        """Return the set of BGC parameter tokens requested by the query.

        Only returns tokens for parameters that belong to ``BGC_PARAMETERS``;
        core parameters (TEMP, PSAL, PRES) are excluded.
        """
        try:
            spec = parsed.validation.spec if parsed.validation else None
            if spec is None:
                return frozenset()
            return frozenset(
                p.value for p in spec.parameters if p in BGC_PARAMETERS
            )
        except Exception:
            return frozenset()

    @staticmethod
    def _requested_direction(parsed) -> str | None:
        """Return ``'A'`` or ``'D'`` when the parsed query explicitly requests
        ascending or descending profiles, or ``None`` otherwise.
        """
        try:
            spec = parsed.validation.spec if parsed.validation else None
            if spec is None or spec.direction is None:
                return None
            return spec.direction.value
        except Exception:
            return None

    @staticmethod
    def _combine_where_filters(
        bgc_where: dict | None, required_direction: str | None
    ) -> dict | None:
        """Combine optional BGC and direction ChromaDB ``where`` filters."""
        clauses: list[dict] = []
        if bgc_where is not None:
            clauses.append(bgc_where)
        if required_direction is not None:
            clauses.append({"direction": required_direction})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    @staticmethod
    def _decode_sql_bgc_params(raw_bgc_params) -> tuple[dict | None, bool]:
        """Safely decode SQL ``bgc_params`` JSON.

        Returns ``(decoded_dict, is_malformed)``.
        ``None`` (SQL NULL) returns ``(None, False)``.
        Malformed JSON or non-dict values return ``(None, True)``.
        """
        if raw_bgc_params is None:
            return None, False
        if isinstance(raw_bgc_params, dict):
            return raw_bgc_params, False
        if isinstance(raw_bgc_params, (str, bytes)):
            try:
                decoded = json.loads(raw_bgc_params)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None, True
            if decoded is None:
                return None, True
            if isinstance(decoded, dict):
                return decoded, False
            return None, True
        return None, True

    @classmethod
    def _sql_has_all_params(
        cls, sql_row: dict, required_params: frozenset[str]
    ) -> bool:
        """Authoritatively verify against SQL ``bgc_params`` that every
        requested BGC parameter key exists and holds a non-empty array.
        """
        decoded, is_malformed = cls._decode_sql_bgc_params(
            sql_row.get("bgc_params")
        )
        if is_malformed or not decoded:
            return False
        for tok in required_params:
            if tok not in decoded:
                return False
            arr = decoded[tok]
            if not isinstance(arr, (list, tuple)) or len(arr) == 0:
                return False
        return True

    @staticmethod
    def _metadata_has_param(metadata: dict, param_token: str) -> bool:
        """Return True if ``param_token`` appears as an exact member of the
        comma-separated ``bgc_parameters`` metadata field.

        CHLA and CHLA_FLUORESCENCE are kept distinct because they are stored
        as separate tokens (e.g. "BBP700,CHLA,DOXY" vs
        "BBP700,CHLA_FLUORESCENCE,DOXY").
        """
        raw = metadata.get("bgc_parameters", "") or ""
        tokens = {t.strip() for t in raw.split(",") if t.strip()}
        return param_token in tokens

    @classmethod
    def _check_metadata_sql_consistency(
        cls, metadata: dict, sql_row: dict
    ) -> list[str]:
        """Return a list of mismatch descriptions between ChromaDB metadata and
        the SQL record.  An empty list means they are consistent.

        Verifies all corresponding fields present in the collection schema:
        profile_id_sql, float_id_sql (WMO), cycle_number, direction, has_bgc,
        and bgc_parameters token set.
        """
        mismatches: list[str] = []

        chroma_pid = metadata.get("profile_id_sql")
        sql_pid = sql_row.get("profile_id")
        if chroma_pid is not None and sql_pid is not None:
            if int(chroma_pid) != int(sql_pid):
                mismatches.append(
                    f"profile_id_sql: ChromaDB={chroma_pid}, SQL={sql_pid}"
                )

        chroma_wmo = metadata.get("float_id_sql")
        sql_wmo = sql_row.get("wmo_number")
        if chroma_wmo is not None and sql_wmo is not None:
            if int(chroma_wmo) != int(sql_wmo):
                mismatches.append(
                    f"float_id_sql: ChromaDB={chroma_wmo}, SQL={sql_wmo}"
                )

        chroma_cycle = metadata.get("cycle_number")
        sql_cycle = sql_row.get("cycle_number")
        if chroma_cycle is not None and sql_cycle is not None:
            if int(chroma_cycle) != int(sql_cycle):
                mismatches.append(
                    f"cycle_number: ChromaDB={chroma_cycle}, SQL={sql_cycle}"
                )

        chroma_dir = metadata.get("direction")
        sql_dir = sql_row.get("direction")
        if chroma_dir is not None and sql_dir is not None:
            if str(chroma_dir).strip() != str(sql_dir).strip():
                mismatches.append(
                    f"direction: ChromaDB={chroma_dir!r}, SQL={sql_dir!r}"
                )

        decoded_bgc, is_malformed = cls._decode_sql_bgc_params(
            sql_row.get("bgc_params")
        )
        if is_malformed:
            mismatches.append("bgc_params: SQL value is malformed or non-object JSON")

        sql_has_bgc = isinstance(decoded_bgc, dict) and len(decoded_bgc) > 0
        chroma_has_bgc = metadata.get("has_bgc")
        if chroma_has_bgc is not None and bool(chroma_has_bgc) != sql_has_bgc:
            mismatches.append(
                f"has_bgc: ChromaDB={chroma_has_bgc}, SQL={'non-empty' if sql_has_bgc else 'null/empty'}"
            )

        if "bgc_parameters" in metadata and metadata.get("bgc_parameters") is not None:
            chroma_tokens = {
                t.strip()
                for t in str(metadata.get("bgc_parameters") or "").split(",")
                if t.strip()
            }
            sql_tokens = (
                set(decoded_bgc.keys()) if isinstance(decoded_bgc, dict) else set()
            )
            if chroma_tokens != sql_tokens:
                mismatches.append(
                    f"bgc_parameters: ChromaDB={sorted(chroma_tokens)}, SQL={sorted(sql_tokens)}"
                )

        return mismatches

    def _retrieve_semantic(
        self,
        question,
        bgc_where: dict | None = None,
        required_params: frozenset[str] | None = None,
        required_direction: str | None = None,
    ):
        """Retrieve relevant profiles using ChromaDB semantic search,
        then verify and obtain the authoritative records from MySQL.

        Parameters
        ----------
        question : str
            The user's question (used for embedding).
        bgc_where : dict | None
            Optional ChromaDB ``where`` filter, e.g. ``{"has_bgc": True}``.
            Pass ``None`` for unrestricted retrieval.
        required_params : frozenset[str] | None
            Optional set of BGC parameter tokens (e.g. ``{"DOXY"}``).
            Candidates are narrowed via ChromaDB metadata and authoritatively
            verified against SQL ``bgc_params`` JSON.
        required_direction : str | None
            Optional profile direction constraint (``'A'`` or ``'D'``).
            Verified against SQL ``p.direction``.
        """

        context_data: list[dict] = []
        effective_where = self._combine_where_filters(
            bgc_where, required_direction
        )

        # --- ChromaDB query -----------------------------------------------
        try:
            query_embedding = (
                self.db_manager.embedding_model
                .encode(question)
                .tolist()
            )

            if required_params or required_direction is not None:
                try:
                    total_vecs = self.db_manager.chroma_collection.count()
                    candidate_limit = max(100, min(int(total_vecs), 1000))
                except Exception:
                    candidate_limit = 100
            else:
                candidate_limit = 5
            query_kwargs: dict = {
                "query_embeddings": [query_embedding],
                "n_results": candidate_limit,
                "include": ["metadatas", "distances"],
            }
            if effective_where is not None:
                query_kwargs["where"] = effective_where

            search_results = self.db_manager.chroma_collection.query(
                **query_kwargs
            )

            ids = search_results.get("ids", [[]])
            if not ids or not ids[0]:
                return []

            metadatas = search_results.get("metadatas", [[]])[0]

        except Exception as e:
            logging.error("Error querying ChromaDB: %s", e)
            raise ChatRetrievalError(
                "Semantic profile search is currently unavailable."
            ) from e

        # --- Exact BGC parameter membership filter (Task B) ----------------
        # Applied against the ChromaDB metadata before touching MySQL, so the
        # SQL retrieval loop only runs for qualifying candidates.
        filtered_metadatas: list[dict] = []
        for metadata in metadatas:
            if not metadata or "profile_id_sql" not in metadata:
                continue
            if required_params:
                if not all(
                    self._metadata_has_param(metadata, tok)
                    for tok in required_params
                ):
                    logging.debug(
                        "Excluding profile_id_sql=%s: missing params %s (has %s)",
                        metadata.get("profile_id_sql"),
                        required_params,
                        metadata.get("bgc_parameters"),
                    )
                    continue
            filtered_metadatas.append(metadata)

        if not filtered_metadatas:
            return []

        # --- MySQL retrieval (Tasks C + D) ---------------------------------
        # Extends the original query with BGC columns and direction.
        sql_query = text(
            """
            SELECT
                p.profile_id,
                p.cycle_number,
                p.profile_time,
                p.latitude,
                p.longitude,
                p.pressure,
                p.temperature,
                p.salinity,
                p.bgc_params,
                p.bgc_qc,
                p.bgc_adjusted,
                p.bgc_adjusted_qc,
                p.bgc_pressure,
                p.bgc_source_metadata,
                p.direction,
                p.data_mode,
                f.wmo_number
            FROM argo_profiles p
            JOIN argo_floats f ON p.float_id = f.float_id
            WHERE p.profile_id = :profile_id
            """
        )

        # Keep a set of SQL profile IDs already added to avoid duplicates.
        seen_profile_ids: set[int] = set()

        try:
            with self.db_manager.mysql_engine.connect() as conn:
                for metadata in filtered_metadatas:
                    profile_id = int(metadata["profile_id_sql"])
                    if profile_id in seen_profile_ids:
                        continue

                    sql_row = conn.execute(
                        sql_query, {"profile_id": profile_id}
                    ).fetchone()

                    if sql_row is None:
                        logging.warning(
                            "ChromaDB references profile_id_sql=%d but it was "
                            "not found in MySQL — skipping.",
                            profile_id,
                        )
                        continue

                    row_dict = dict(sql_row._mapping)

                    # Metadata consistency check (Task D / Phase 2.1).
                    mismatches = self._check_metadata_sql_consistency(
                        metadata, row_dict
                    )
                    if mismatches:
                        logging.warning(
                            "Metadata/SQL mismatch for profile_id=%d: %s — "
                            "excluding from result set.",
                            profile_id,
                            "; ".join(mismatches),
                        )
                        continue

                    # Authoritative SQL-side BGC parameter membership check.
                    if required_params and not self._sql_has_all_params(
                        row_dict, required_params
                    ):
                        logging.warning(
                            "SQL record for profile_id=%d does not contain all "
                            "requested BGC parameters %s — excluding.",
                            profile_id,
                            sorted(required_params),
                        )
                        continue

                    # Authoritative SQL-side direction check.
                    if required_direction is not None:
                        sql_dir = str(row_dict.get("direction") or "").strip()
                        if sql_dir != required_direction:
                            logging.warning(
                                "SQL record for profile_id=%d has direction=%r, "
                                "expected %r — excluding.",
                                profile_id,
                                sql_dir,
                                required_direction,
                            )
                            continue

                    # Annotate with ChromaDB metadata fields (product_type,
                    # has_bgc, bgc_parameters, bgc_parameter_count) so that
                    # _build_compact_context can surface them without an extra
                    # SQL join.
                    row_dict["_meta_has_bgc"] = metadata.get("has_bgc")
                    row_dict["_meta_product_type"] = metadata.get("product_type")
                    row_dict["_meta_bgc_parameters"] = metadata.get("bgc_parameters")
                    row_dict["_meta_bgc_parameter_count"] = metadata.get(
                        "bgc_parameter_count"
                    )

                    seen_profile_ids.add(profile_id)
                    context_data.append(row_dict)
                    if len(context_data) >= 5:
                        break

        except Exception as e:
            logging.error("Error retrieving semantic profiles: %s", e)
            raise ChatRetrievalError(
                "Semantic profile search could not be completed."
            ) from e

        return context_data

    # ============================================================
    # SEMANTIC-ANSWER SAFEGUARDS (Phase 4D)
    # ============================================================

    #: Analysis-shaped requests the current application cannot execute.
    #: Retrieval + Qwen must not be presented as if the analysis had run.
    _ANALYSIS_REQUESTS = (
        (
            r"\b(?:most|least)\s+(?:anomalous|unusual|abnormal|extreme|"
            r"suspicious)\b|\brank(?:ing|ed)?\b",
            "rank floats or profiles by anomalous measurements",
        ),
        (
            r"\banomal(?:y|ies|ous)\b|\bsuspicious\b|\bunusual(?:ly)?\b|"
            r"\bstrangely\b|\boddly\b|\boutliers?\b",
            "detect statistically anomalous or unusual profiles",
        ),
        (
            r"\b(?:higher|lower|warmer|colder|saltier|fresher)\s+than\b|"
            r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b",
            "compare regions, floats or time periods",
        ),
        (
            r"\b(?:trend|climate change|seasonal|seasonality|patterns?)\b",
            "calculate trends, seasonal behaviour or recurring patterns",
        ),
        (
            r"\binstrument errors?|\bsensor errors?|\bmalfunction\w*\b|"
            r"\bcalibration errors?\b",
            "identify instrument, sensor or calibration errors",
        ),
        (
            r"\b(?:average|mean|maximum|minimum)\b",
            "calculate a dataset-wide statistic",
        ),
        (
            r"\b(?:statistically|statistical significance|significant "
            r"difference|correlat\w*|caus(?:e|ed|ation)|relationship|"
            r"relate[sd]?|how variable|variability)\b",
            "run a statistical or causal analysis",
        ),
        (
            r"\bdataset[- ]wide\b|\bhow variable\b|"
            r"\bsummar(?:y|ise|ize)\b[^?]{0,30}\bdataset\b",
            "calculate dataset-wide statistics",
        ),
    )

    def _unsupported_analysis_answer(self, question):
        """Return an explicit limitation for analysis the app cannot run.

        ``None`` means the question is an ordinary discovery request and the
        existing retrieval workflow may answer it.  This method never touches
        the database and never calls the language model.
        """

        text = " ".join(question.lower().split())

        for pattern, operation in self._ANALYSIS_REQUESTS:

            if re.search(pattern, text):

                return (
                    f"I cannot {operation} from the stored profile "
                    "metadata: that analysis is not implemented, and I will "
                    "not present a few retrieved profiles as if it had been "
                    "performed. I can list profiles that contain a "
                    "parameter, show profile metadata for a WMO number or "
                    "cycle, or report profile counts.",
                    [],
                )

        return None

    def _float_distribution_limitation(self, question):
        """A per-year/month float count is not a plain float enumeration.

        Phase 4E: "how many floats were active each year" reached the float
        enumeration route and was answered with the total float list, which
        is a different question.  Counting distinct floats per period is not
        implemented, so it is stated instead of silently substituted.
        """

        text = " ".join(question.lower().split())

        if re.search(
            r"\b(?:each|per|every)\s+(?:year|month)\b|"
            r"\b(?:yearly|monthly|annually)\b",
            text,
        ):
            return (
                "I can list the floats present in the dataset, but counting "
                "how many distinct floats were active per year or month is "
                "not implemented. I can report the total number of floats, "
                "the profiles for one WMO number, or the profile count per "
                "year.",
                [],
            )

        return None

    def _orphan_removal_clarification(self, question):
        """"Remove the temperature filter" with nothing to remove.

        Without a previous structured answer there is no filter to remove;
        answering with temperature profiles would invert the request.
        """

        text = " ".join(question.lower().split()).strip(" ?.!")

        if re.match(
            r"^(?:please\s+)?(?:remove|drop|clear)\s+(?:the\s+)?"
            r"(?:temperature|salinity|pressure|oxygen|chlorophyll|nitrate|"
            r"ph|date|year|time|cycles?|data[\s-]?mode)\b",
            text,
        ):
            return (
                "There is no earlier filter in this conversation to remove. "
                "State the filter you want (for example 'show profiles "
                "containing salinity'), or ask this after a filtered question.",
                [],
            )

        return None

    def _contextless_count_clarification(self, question):
        """A bare count must not be answered with the retrieval sample size."""

        text = " ".join(question.lower().split()).strip(" ?.!")

        if text in {"how many", "how many are there", "how many profiles", "count them"}:
            return (
                "How many of what? Ask, for example, 'how many profiles are "
                "there?', 'how many floats are there?' or 'how many delayed "
                "profiles are there?'.",
                [],
            )

        return None

    # ============================================================
    # NUMERICAL PROFILE PROCESSING
    # ============================================================

    def _profile_summary(self, profile):
        """
        Create a compact, factual summary of one ARGO profile.

        This is generated entirely by Python. Statistics are computed from
        valid numeric observations only; missing values stay missing and are
        never converted to zero.
        """

        pressure = self._parse_array(
            profile.get("pressure")
        )

        temperature = self._parse_array(
            profile.get("temperature")
        )

        salinity = self._parse_array(
            profile.get("salinity")
        )

        summary = {
            "profile_id": profile.get("profile_id"),
            "wmo_number": profile.get("wmo_number"),
            "cycle_number": profile.get("cycle_number"),
            "profile_time": profile.get("profile_time"),
            "latitude": profile.get("latitude"),
            "longitude": profile.get("longitude"),
            "pressure_count": len(pressure),
            "temperature_count": len(temperature),
            "salinity_count": len(salinity),
            # BGC product metadata (Task D)
            "direction": profile.get("direction"),
            "has_bgc": profile.get("_meta_has_bgc"),
            "product_type": profile.get("_meta_product_type"),
            "bgc_parameters": profile.get("_meta_bgc_parameters"),
            "bgc_parameter_count": profile.get("_meta_bgc_parameter_count"),
        }

        # BGC measurement arrays (Task C) — parsed but not sent to Qwen3 raw.
        # Store level count and pressure range to inform the context summary.
        bgc_pressure_raw = profile.get("bgc_pressure")
        bgc_pressure = self._parse_array(bgc_pressure_raw) if bgc_pressure_raw else []
        summary["bgc_pressure_count"] = len(bgc_pressure)
        if bgc_pressure:
            valid_bgc_pres = self._valid_numbers(bgc_pressure)
            summary["bgc_pressure_min"] = (
                self._round_number(min(valid_bgc_pres)) if valid_bgc_pres else None
            )
            summary["bgc_pressure_max"] = (
                self._round_number(max(valid_bgc_pres)) if valid_bgc_pres else None
            )
        else:
            summary["bgc_pressure_min"] = None
            summary["bgc_pressure_max"] = None

        # BGC parameter values: decode the JSON dict (keys=param names,
        # values=arrays) and compute validated statistics via bgc_calculations.
        bgc_params_raw = profile.get("bgc_params")
        bgc_param_counts: dict[str, int] = {}
        bgc_stats: dict[str, dict] = {}
        if bgc_params_raw:
            try:
                bgc_dict = (
                    bgc_params_raw
                    if isinstance(bgc_params_raw, dict)
                    else json.loads(bgc_params_raw)
                )
                for param_key, param_arr in bgc_dict.items():
                    parsed_arr = self._parse_array(param_arr) if param_arr else []
                    valid_arr = self._valid_numbers(parsed_arr)
                    bgc_param_counts[param_key] = len(valid_arr)
                    calc = calculate_bgc_statistics(
                        [profile],
                        param_key,
                        scope="profile",
                        aggregation_type="SUMMARY",
                    )
                    bgc_stats[param_key] = calc.to_dict()
            except Exception:
                pass  # Malformed JSON — treat as no BGC data
        summary["bgc_param_counts"] = bgc_param_counts
        summary["bgc_stats"] = bgc_stats

        for name, values in (
            ("pressure", pressure),
            ("temperature", temperature),
            ("salinity", salinity),
        ):
            valid = self._valid_numbers(values)

            summary[f"{name}_valid_count"] = len(valid)
            summary[f"{name}_missing_count"] = len(values) - len(valid)

            if values:
                summary[f"{name}_first"] = self._round_number(values[0])
                summary[f"{name}_last"] = self._round_number(values[-1])

            if valid:
                summary[f"{name}_min"] = self._round_number(min(valid))
                summary[f"{name}_max"] = self._round_number(max(valid))
                summary[f"{name}_mean"] = self._round_number(
                    sum(valid) / len(valid)
                )
            else:
                summary[f"{name}_min"] = None
                summary[f"{name}_max"] = None
                summary[f"{name}_mean"] = None

        return summary

    # ============================================================
    # DIRECT NUMERICAL ANSWERS
    # ============================================================

    def _build_direct_profile_answer(
        self,
        question,
        context_data
    ):
        """
        Produce a factual answer for direct profile requests.

        Python generates the numerical facts.
        No LLM is needed for this operation.
        """

        variable = self._requested_variable(question)

        if not variable:
            return None

        if not context_data:
            return None

        profile = context_data[0]

        summary = self._profile_summary(profile)

        wmo = summary.get("wmo_number", "N/A")
        cycle = summary.get("cycle_number", "N/A")
        profile_time = summary.get("profile_time", "N/A")

        if variable == "temperature":

            valid_count = summary.get(
                "temperature_valid_count",
                0
            )

            if valid_count == 0:
                return (
                    f"WMO {wmo} profile (cycle {cycle}) contains "
                    f"{summary.get('temperature_count', 0)} temperature "
                    f"levels, but none holds a valid numeric observation. "
                    f"Temperature cannot be summarised."
                )

            first_pressure = summary.get(
                "pressure_first",
                "N/A"
            )

            last_pressure = summary.get(
                "pressure_last",
                "N/A"
            )

            first_temp = summary.get(
                "temperature_first",
                "N/A"
            )

            last_temp = summary.get(
                "temperature_last",
                "N/A"
            )

            minimum = summary.get("temperature_min")
            maximum = summary.get("temperature_max")
            mean = summary.get("temperature_mean")
            missing = summary.get("temperature_missing_count", 0)

            missing_note = (
                f" {missing} of "
                f"{summary.get('temperature_count', 0)} levels "
                f"have no observation."
                if missing
                else ""
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{valid_count} valid temperature measurements."
                f"{missing_note}\n\n"
                f"Temperature range: {minimum} to {maximum} °C "
                f"(mean {mean} °C).\n"
                f"First recorded pair: pressure "
                f"{self._fmt_value(first_pressure)}, temperature "
                f"{self._fmt_value(first_temp)}.\n"
                f"Last recorded pair: pressure "
                f"{self._fmt_value(last_pressure)}, temperature "
                f"{self._fmt_value(last_temp)}.\n\n"
                f"The complete measurement data is available "
                f"to the visualization layer."
            )

        if variable == "salinity":

            valid_count = summary.get(
                "salinity_valid_count",
                0
            )

            if valid_count == 0:
                return (
                    f"WMO {wmo} profile (cycle {cycle}) contains "
                    f"{summary.get('salinity_count', 0)} salinity "
                    f"levels, but none holds a valid numeric observation. "
                    f"Salinity cannot be summarised."
                )

            minimum = summary.get("salinity_min")
            maximum = summary.get("salinity_max")
            mean = summary.get("salinity_mean")
            missing = summary.get("salinity_missing_count", 0)

            missing_note = (
                f" {missing} of "
                f"{summary.get('salinity_count', 0)} levels "
                f"have no observation."
                if missing
                else ""
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{valid_count} valid salinity measurements."
                f"{missing_note}\n\n"
                f"Salinity range: {minimum} to {maximum} PSU "
                f"(mean {mean} PSU)."
            )

        if variable == "pressure":

            valid_count = summary.get(
                "pressure_valid_count",
                0
            )

            if valid_count == 0:
                return (
                    f"WMO {wmo} profile (cycle {cycle}) contains "
                    f"{summary.get('pressure_count', 0)} pressure "
                    f"levels, but none holds a valid numeric observation. "
                    f"Pressure cannot be summarised."
                )

            minimum = summary.get("pressure_min")
            maximum = summary.get("pressure_max")
            mean = summary.get("pressure_mean")
            missing = summary.get("pressure_missing_count", 0)

            missing_note = (
                f" {missing} of "
                f"{summary.get('pressure_count', 0)} levels "
                f"have no observation."
                if missing
                else ""
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{valid_count} valid pressure measurements."
                f"{missing_note}\n\n"
                f"Pressure range: {minimum} to {maximum} dbar "
                f"(mean {mean} dbar)."
            )

        return None

    # ============================================================
    # COMPACT LLM CONTEXT
    # ============================================================

    def _build_compact_context(
        self,
        context_data
    ):
        """
        Build compact factual context for Qwen3.

        Raw measurement arrays are deliberately NOT sent to the LLM
        for general questions.
        """

        context_lines = []

        for index, profile in enumerate(
            context_data,
            start=1
        ):

            summary = self._profile_summary(profile)

            context_lines.append(
                f"Profile {index}:"
            )

            context_lines.append(
                f"WMO number: "
                f"{summary.get('wmo_number', 'N/A')}"
            )

            context_lines.append(
                f"Cycle number: "
                f"{summary.get('cycle_number', 'N/A')}"
            )

            context_lines.append(
                f"Profile time: "
                f"{summary.get('profile_time', 'N/A')}"
            )

            context_lines.append(
                f"Latitude: "
                f"{summary.get('latitude', 'N/A')}"
            )

            context_lines.append(
                f"Longitude: "
                f"{summary.get('longitude', 'N/A')}"
            )

            for name, label, unit in (
                ("temperature", "Temperature", "degrees Celsius"),
                ("salinity", "Salinity", "PSU"),
                ("pressure", "Pressure", "dbar"),
            ):
                valid = summary.get(f"{name}_valid_count", 0)

                if not valid:
                    context_lines.append(
                        f"No valid {label.lower()} measurements are "
                        f"available for this profile "
                        f"({summary.get(f'{name}_count', 0)} levels, all missing)."
                    )
                    continue

                context_lines.append(
                    f"Valid {label.lower()} measurements: {valid} of "
                    f"{summary.get(f'{name}_count', 0)} levels "
                    f"(unit: {unit})"
                )
                context_lines.append(
                    f"{label} range: {summary[f'{name}_min']} to "
                    f"{summary[f'{name}_max']} {unit} "
                    f"(mean {summary.get(f'{name}_mean')} {unit})"
                )
            # BGC product metadata (Phase 2 — Task D)
            direction = summary.get("direction")
            if direction:
                context_lines.append(
                    f"Profile direction: {'ascending' if direction == 'A' else 'descending'}"
                )
            product_type = summary.get("product_type")
            if product_type:
                context_lines.append(f"Product type: {product_type}")
            bgc_params_str = summary.get("bgc_parameters")
            if bgc_params_str:
                context_lines.append(
                    f"BGC parameters present: {bgc_params_str}"
                )
            # BGC pressure axis (Phase 2 — Task C)
            bgc_pres_count = summary.get("bgc_pressure_count", 0)
            if bgc_pres_count:
                bgc_pmin = summary.get("bgc_pressure_min")
                bgc_pmax = summary.get("bgc_pressure_max")
                context_lines.append(
                    f"BGC pressure axis: {bgc_pres_count} levels"
                    + (
                        f", range {bgc_pmin} to {bgc_pmax} dbar"
                        if bgc_pmin is not None and bgc_pmax is not None
                        else ""
                    )
                )
            # BGC parameter validated statistics (Phase 3A/3C)
            bgc_stats = summary.get("bgc_stats") or {}
            if bgc_stats:
                for param_key in sorted(bgc_stats):
                    st_dict = bgc_stats[param_key]
                    unit = st_dict.get("unit") or BGC_PARAMETER_UNITS.get(param_key, "")
                    valid_cnt = st_dict.get("valid_count", 0)
                    missing_cnt = st_dict.get("missing_count", 0)
                    if valid_cnt == 0:
                        context_lines.append(
                            f"BGC {param_key}: 0 valid levels "
                            f"({missing_cnt} missing, unit: {unit})"
                        )
                    else:
                        context_lines.append(
                            f"BGC {param_key}: {valid_cnt} valid levels "
                            f"({missing_cnt} missing, unit: {unit}, "
                            f"qc_policy: {st_dict.get('qc_policy')}), "
                            f"min={st_dict.get('minimum')}, max={st_dict.get('maximum')}, "
                            f"mean={st_dict.get('mean')}, median={st_dict.get('median')}, "
                            f"std_dev={st_dict.get('std_dev')}"
                        )
                    for w in st_dict.get("warnings") or ():
                        context_lines.append(f"BGC {param_key} note: {w}")
            else:
                bgc_param_counts = summary.get("bgc_param_counts") or {}
                if bgc_param_counts:
                    for param_key, valid_cnt in sorted(bgc_param_counts.items()):
                        context_lines.append(
                            f"BGC {param_key}: {valid_cnt} valid levels"
                        )
            context_lines.append("")

        notes = []

        for index, profile in enumerate(context_data, start=1):

            summary = self._profile_summary(profile)
            issues = []

            salinity_min = summary.get("salinity_min")
            salinity_max = summary.get("salinity_max")
            if salinity_min is not None and salinity_min < 30:
                issues.append(f"salinity as low as {salinity_min} PSU")
            if salinity_max is not None and salinity_max > 40:
                issues.append(f"salinity as high as {salinity_max} PSU")

            temperature_min = summary.get("temperature_min")
            temperature_max = summary.get("temperature_max")
            if temperature_min is not None and temperature_min < -2:
                issues.append(f"temperature as low as {temperature_min} C")
            if temperature_max is not None and temperature_max > 40:
                issues.append(f"temperature as high as {temperature_max} C")

            pressure_min = summary.get("pressure_min")
            if pressure_min is not None and pressure_min < 0:
                issues.append(f"negative pressure {pressure_min} dbar")

            if issues:
                notes.append(
                    f"Profile {index} contains values outside the usual "
                    f"oceanographic range ({', '.join(issues)})."
                )

        if notes:
            context_lines.append(
                "DATA-QUALITY NOTE (computed by the application, not the "
                "language model): these are raw ARGO measurements. "
                + " ".join(notes)
                + " Treat them as unvalidated and possibly implausible; do "
                "not present them as normal ocean conditions."
            )

        return "\n".join(context_lines)

    # ============================================================
    # LLM PROMPT
    # ============================================================

    def _build_prompt(
        self,
        question,
        context_data
    ):
        """
        Build a compact, strongly grounded prompt for Qwen3.
        """

        compact_context = (
            self._build_compact_context(
                context_data
            )
        )

        return f"""
You are the language-generation component of an ARGO
oceanographic data application.

Answer the user's question using ONLY the factual information
provided in the retrieved context.

Do not invent measurements, dates, identifiers, units,
depths, causes, or scientific conclusions.

IMPORTANT:

1. Pressure is NOT automatically depth.

2. Do not convert pressure into depth.

3. Do not invent units that are not explicitly provided.

4. Do not invent measurements.

5. Do not invent timestamps for individual measurements.

6. Do not treat profile IDs, WMO numbers, cycle numbers,
   pressure values, or temperatures as interchangeable.

7. If information is not present in the context, say that
   it is not available.

8. If multiple profiles are present, keep them separate.

9. Do not claim a scientific cause or explanation unless
   the supplied context supports it.

10. When reporting BGC statistics (minimum, maximum, mean, median,
    standard deviation), report only the exact values, units,
    valid/missing counts, and QC notes provided in the context. Do not
    recompute, round differently, or claim statistical significance.

11. Do not extrapolate beyond the supplied information.

12. Keep the answer concise and directly answer the question.

12. The numerical values in the context were calculated or
    retrieved by the application. Do not modify them.

13. Do not generalise from the retrieved examples to the whole
    dataset, a region, a season or a time period.

14. Pressure is measured in dbar. Never describe a pressure value
    as shallow, near-surface, surface or deep, and never convert
    pressure to depth. Never write phrases such as 'shallow
    depth', 'near-surface pressure' or 'deep pressure'. Describe
    pressure only as a number of dbar.

15. These are raw ARGO measurements. Do not describe them as
    scientifically validated or quality controlled unless the
    context explicitly says so.

16. If a value looks physically implausible (salinity far outside
    30-40 PSU, temperature outside -2 to 40 C, negative
    pressure), say that it appears implausible or unvalidated and
    do not interpret it as normal ocean conditions.

17. Do not claim that data passed quality control when QC
    information is not in the context.

18. State how many profiles the context contains when making any
    statement about them.

19. If the question asks for a comparison, ranking, anomaly,
    trend, seasonal pattern or instrument error that the context
    cannot establish, say it cannot be determined from the
    retrieved examples.

The retrieved context contains {len(context_data)} profile summaries. They are
examples selected by similarity, NOT a statistically representative sample of
the dataset.

RETRIEVED FACTUAL CONTEXT:

{compact_context}

USER QUESTION:

{question}

ANSWER:
"""

    # ============================================================
    # DETERMINISTIC QUERYSPEC PIPELINE (Phase 4B)
    # ============================================================

    def _answer_with_query_spec(self, question, strict=False):
        """Try the deterministic QuerySpec pipeline for a structured question.

        Returns ``None`` when the question is outside the deterministic
        pipeline's supported scope, in which case the existing semantic
        retrieval and Qwen answer generation run unchanged.  The pipeline is
        executed at most once, and only when it can actually plan the query,
        so falling back never repeats a database execution.

        ``strict`` asks for an explicit statement when a detected constraint
        cannot be applied, instead of an answer that quietly ignores it.
        """

        try:
            outcome = answer_deterministically(
                question,
                self.db_manager.mysql_engine,
                strict=strict,
            )
        except DeterministicPipelineError as exc:
            logging.error(
                "Deterministic QuerySpec execution failed: %s",
                type(exc).__name__,
            )
            raise ChatRetrievalError(
                "The structured query could not be executed; no result was "
                "produced."
            ) from exc

        if outcome is None:
            logging.info(
                "Deterministic QuerySpec pipeline did not handle the "
                "question; using the existing route."
            )

        return outcome

    def _answer_wmo_refinement(self, question):
        """Answer a WMO question that carries another structured constraint.

        Returns ``(answer, data)`` when the question has such a constraint, and
        ``None`` for a bare identifier lookup, which the existing WMO route
        keeps answering.  A constraint the pipeline cannot apply is reported
        explicitly rather than dropped.
        """

        if not has_structured_refinement(question):
            return None

        outcome = self._answer_with_query_spec(question, strict=True)

        if outcome is None:
            return None

        logging.info(
            "Answered WMO refinement via the deterministic QuerySpec "
            "pipeline (status=%s, rows=%d).",
            outcome.status,
            len(outcome.rows),
        )

        return outcome.answer, list(outcome.rows)

    # ============================================================
    # MAIN RAG PIPELINE
    # ============================================================

    def answer_question(self, question: str):
        """
        Main question-answering pipeline.
        """

        if not question or not question.strip():

            return (
                "Please provide a question.",
                []
            )

        question = question.strip()

        logging.info(
            f"Received question: {question}"
        )

        count_clarification = self._contextless_count_clarification(question)

        if count_clarification is not None:
            logging.info(
                "Clarified a context-free count instead of "
                "reporting a retrieval sample size."
            )
            return count_clarification

        orphan_removal = self._orphan_removal_clarification(question)

        if orphan_removal is not None:
            logging.info(
                "Clarified a filter removal with no earlier context."
            )
            return orphan_removal

        context_data = []
        decision = ArgoQueryRouter.route(question)
        # --------------------------------------------------------
        # 0. General conversation (no database retrieval)
        # --------------------------------------------------------

        if decision.route is Route.GENERAL_CONVERSATION:

            conversational_responses = {
                "hi": "Hello! How can I help you explore oceanographic data?",
                "hello": "Hello! What would you like to know about ARGO data?",
                "hey": "Hey! What can I help you discover about oceanographic data?",
                "thanks": "You're welcome! Let me know if you need anything else.",
                "thank you": "You're welcome! Happy to help.",
                "bye": "Goodbye! Have a great day.",
            }

            normalized = " ".join(question.lower().strip().split())
            normalized = re.sub(r"[!?.]+$", "", normalized)

            answer = conversational_responses.get(
                normalized,
                "Hello! How can I assist you with oceanographic data?"
            )

            return answer, []
        
        logging.info(
            "Query routed to %s: %s",
            decision.route.value,
            decision.reason,
        )

        # --------------------------------------------------------
        # 1. Exact relational routes (MySQL)
        # --------------------------------------------------------
        if decision.route is Route.MYSQL_PROFILE_COUNTS:
            profile_counts = self._retrieve_profile_counts_by_wmo()

            answer = self._build_profile_counts_answer(
                profile_counts
            )

            logging.info(
                "Answered profile-count request directly from MySQL."
            )

            return (
                answer,
                []
            )

        if decision.route is Route.MYSQL_WMO_PROFILE_COUNT:

            # A count question may also carry filters (date, parameter, ...)
            # that this exact route cannot express.
            refined = self._answer_wmo_refinement(question)
            if refined is not None:
                return refined

            count = self._count_profiles_by_wmo(decision.wmo_number)
            return (f"WMO {decision.wmo_number} contains {count} profiles.", [])

        if decision.route is Route.MYSQL_REGIONAL_TREND:
            variable, start_year, end_year = self._parse_arabian_sea_trend_request(question)
            values_by_year = self._retrieve_arabian_sea_trend(variable, start_year, end_year)
            return (
                self._build_arabian_sea_trend_answer(
                    variable, start_year, end_year, values_by_year
                ),
                [],
            )

        if decision.route is Route.MYSQL_FLOAT_ENUMERATION:

            distribution = self._float_distribution_limitation(question)
            if distribution is not None:
                logging.info(
                    "Refused a per-period float count instead of "
                    "answering with the total float list."
                )
                return distribution

            wmo_numbers = self._retrieve_available_wmos()

            answer = self._build_float_enumeration_answer(
                wmo_numbers
            )

            logging.info(
                "Answered float enumeration request directly from MySQL."
            )

            return (
                answer,
                []
            )

        # --------------------------------------------------------
        # 2. Named float (MySQL) or semantic discovery (ChromaDB)
        # --------------------------------------------------------
        if decision.route is Route.MYSQL_WMO:

            # The legacy branch understands the identifier, "latest" and an
            # explicit N only.  Any other constraint must be answered exactly
            # or refused, never silently dropped.
            refined = self._answer_wmo_refinement(question)
            if refined is not None:
                return refined

            wmo_number = decision.wmo_number

            logging.info(
                f"Detected WMO number: {wmo_number}"
            )

            if self._is_latest_request(question):
                total_count = self._count_profiles_by_wmo(wmo_number)
                latest_limit = self._requested_profile_limit(question) or 1
                context_data = self._retrieve_by_wmo(
                    wmo_number, latest_limit, newest_first=True
                )

                if not context_data:
                    return (
                        f"I couldn't find any profiles for WMO number {wmo_number}.",
                        [],
                    )

                return (
                    self._build_latest_profile_answer(
                        wmo_number, total_count, context_data
                    ),
                    context_data,
                )

            if self._is_profile_list_request(question):
                total_count = self._count_profiles_by_wmo(wmo_number)
                requested_limit = self._requested_profile_limit(question)
                retrieval_limit = requested_limit or total_count
                context_data = self._retrieve_by_wmo(wmo_number, retrieval_limit)

                if not context_data:
                    return (
                        f"I couldn't find any profiles for WMO number {wmo_number}.",
                        [],
                    )

                return (
                    self._build_profile_list_answer(
                        wmo_number, total_count, len(context_data)
                    ),
                    context_data,
                )

            context_data = self._retrieve_by_wmo(wmo_number, limit=5)

            if not context_data:

                return (
                    f"I couldn't find any profiles for "
                    f"WMO number {wmo_number}.",
                    []
                )

        else:

            # Phase 4B: structured questions the deterministic pipeline can
            # answer exactly are handled here, before semantic retrieval.
            # A structured constraint the planner cannot execute must be
            # refused here, not handed to ChromaDB/Qwen3 where the answer can
            # imply the filter was applied.
            deterministic = self._answer_with_query_spec(
                question, strict=has_structured_refinement(question)
            )

            # Phase 4D: a question that asks for analysis the application
            # cannot compute must not reach retrieval, where a few unrelated
            # profiles would look like an answer.
            #
            # Phase 4E: the guard is computed before the deterministic answer
            # is returned.  "Detect anomalies in the salinity data" parses as
            # a valid parameter list, so answering it deterministically
            # produced "Found 5 profiles matching the request" and the guard
            # below never ran.  An explicit refusal still outranks the guard,
            # because it names the unsupported operation more precisely.
            guard = self._unsupported_analysis_answer(question)

            # Phase 3: a BGC calculation or numeric-filter answer is a
            # genuinely computed Python result, not a spurious profile list.
            # It must win over the guard unconditionally when successfully
            # executed, so the guard check is bypassed for those shapes.
            _is_bgc_computed = (
                deterministic is not None
                and deterministic.status == "executed"
                and deterministic.result_shape in (
                    "bgc_calculation",
                    "bgc_numeric_filter",
                )
            )

            if deterministic is not None and not (
                guard is not None
                and deterministic.status == "executed"
                and not _is_bgc_computed
            ):

                logging.info(
                    "Answered via the deterministic QuerySpec pipeline "
                    "(status=%s, rows=%d).",
                    deterministic.status,
                    len(deterministic.rows),
                )

                return (
                    deterministic.answer,
                    list(deterministic.rows)
                )

            if guard is not None:

                logging.info(
                    "Refused analysis-shaped question explicitly "
                    "instead of substituting retrieved examples."
                )

                return guard

            # Phase 2 / 2.1: derive BGC metadata filter, parameter membership,
            # and direction constraints from the parsed question.
            _parsed_q = parse_query(question)
            _bgc_where = self._bgc_where_filter(_parsed_q)
            _required_params = self._requested_bgc_params(_parsed_q)
            _required_direction = self._requested_direction(_parsed_q)

            if _bgc_where is not None:
                logging.info(
                    "Semantic retrieval: applying ChromaDB filter %s",
                    _bgc_where,
                )
            if _required_params:
                logging.info(
                    "Semantic retrieval: requiring BGC params %s",
                    _required_params,
                )
            if _required_direction is not None:
                logging.info(
                    "Semantic retrieval: requiring direction %s",
                    _required_direction,
                )

            if (
                _bgc_where is not None
                or _required_params
                or _required_direction is not None
            ):
                context_data = self._retrieve_semantic(
                    question,
                    bgc_where=_bgc_where,
                    required_params=_required_params if _required_params else None,
                    required_direction=_required_direction,
                )
            else:
                context_data = self._retrieve_semantic(question)

            if not context_data:

                return (
                    "I couldn't find ARGO profiles relevant "
                    "to your question.",
                    []
                )

        # --------------------------------------------------------
        # 6. Direct numerical/profile questions
        # --------------------------------------------------------

        direct_answer = None
        if self._contains_profile_request(question):
            direct_answer = self._build_direct_profile_answer(
                question,
                context_data
            )

        if direct_answer is not None:

            logging.info(
                "Answered profile request directly "
                "using Python-generated numerical facts."
            )

            return (
                direct_answer,
                context_data
            )

        # --------------------------------------------------------
        # 7. General questions → Qwen3
        # --------------------------------------------------------

        prompt = self._build_prompt(
            question,
            context_data
        )

        try:

            response = ollama.generate(
                model="qwen3:8b",
                prompt=prompt,
                stream=False,
                think=False,
                options={
                    "temperature": 0.1
                }
            )

            answer = response.get(
                "response",
                ""
            ).strip()

            if not answer:

                return (
                    "I retrieved the relevant ARGO data, "
                    "but the language model did not return "
                    "an answer.",
                    context_data
                )

            logging.info(
                "Qwen3 generated response successfully."
            )

            return (
                answer,
                context_data
            )

        except Exception as e:

            logging.error(
                f"Error communicating with Qwen3: {e}"
            )

            return (
                "I retrieved the relevant ARGO data, "
                "but I could not generate the natural-language "
                "response.",
                context_data
            )
