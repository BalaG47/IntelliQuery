import json
import logging
import math
import re

import ollama
import streamlit as st
from sqlalchemy import text

from database_manager import DatabaseManager
from query_router import ArgoQueryRouter, Route


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
        """Read an explicit 'first N' or 'top N' profile request."""

        match = re.search(
            r"\b(?:first|top)\s+(\d+)\s+profiles?\b",
            question.lower(),
        )

        return int(match.group(1)) if match else None

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
            return []

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
            return []

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
            return 0

    def _retrieve_by_wmo(self, wmo_number, limit):
        """
        Retrieve chronological profiles for a specific WMO.

        ``limit`` is intentionally explicit. General questions use a small
        context window, while an explicit list request can request all
        profiles or its own first-N window.
        """

        context_data = []

        query = text(
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
                f.wmo_number
            FROM argo_profiles p
            JOIN argo_floats f
                ON p.float_id = f.float_id
            WHERE f.wmo_number = :wmo_number
            ORDER BY p.profile_time
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
            return {}

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

    def _retrieve_semantic(self, question):
        """
        Retrieve relevant profiles using ChromaDB semantic search,
        then obtain the authoritative records from MySQL.
        """

        context_data = []

        try:

            query_embedding = (
                self.db_manager.embedding_model
                .encode(question)
                .tolist()
            )

            search_results = (
                self.db_manager.chroma_collection.query(
                    query_embeddings=[query_embedding],
                    n_results=5
                )
            )

            ids = search_results.get("ids", [[]])

            if not ids or not ids[0]:
                return []

            metadatas = search_results.get("metadatas", [[]])

            profile_sql_ids = []

            for metadata in metadatas[0]:

                if metadata and "profile_id_sql" in metadata:

                    profile_sql_ids.append(
                        int(metadata["profile_id_sql"])
                    )

        except Exception as e:

            logging.error(
                f"Error querying ChromaDB: {e}"
            )

            return []

        query = text(
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
                f.wmo_number
            FROM argo_profiles p
            JOIN argo_floats f
                ON p.float_id = f.float_id
            WHERE p.profile_id = :profile_id
            """
        )

        try:

            with self.db_manager.mysql_engine.connect() as conn:

                for profile_id in profile_sql_ids:

                    result = conn.execute(
                        query,
                        {"profile_id": profile_id}
                    ).fetchone()

                    if result:

                        context_data.append(
                            dict(result._mapping)
                        )

        except Exception as e:

            logging.error(
                f"Error retrieving semantic profiles: {e}"
            )

        return context_data

    # ============================================================
    # NUMERICAL PROFILE PROCESSING
    # ============================================================

    def _profile_summary(self, profile):
        """
        Create a compact, factual summary of one ARGO profile.

        This is generated entirely by Python.
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
        }

        if pressure:

            summary["pressure_first"] = (
                self._round_number(pressure[0])
            )

            summary["pressure_last"] = (
                self._round_number(pressure[-1])
            )

            summary["pressure_min"] = (
                self._round_number(min(pressure))
            )

            summary["pressure_max"] = (
                self._round_number(max(pressure))
            )

        if temperature:

            summary["temperature_first"] = (
                self._round_number(temperature[0])
            )

            summary["temperature_last"] = (
                self._round_number(temperature[-1])
            )

            summary["temperature_min"] = (
                self._round_number(min(temperature))
            )

            summary["temperature_max"] = (
                self._round_number(max(temperature))
            )

        if salinity:

            summary["salinity_first"] = (
                self._round_number(salinity[0])
            )

            summary["salinity_last"] = (
                self._round_number(salinity[-1])
            )

            summary["salinity_min"] = (
                self._round_number(min(salinity))
            )

            summary["salinity_max"] = (
                self._round_number(max(salinity))
            )

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

            count = summary.get(
                "temperature_count",
                0
            )

            if count == 0:
                return (
                    f"No temperature measurements were available "
                    f"for WMO {wmo}."
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

            minimum = summary.get(
                "temperature_min",
                "N/A"
            )

            maximum = summary.get(
                "temperature_max",
                "N/A"
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{count} temperature measurements.\n\n"
                f"Temperature range: {minimum} to {maximum}.\n"
                f"First recorded pair: pressure "
                f"{first_pressure}, temperature {first_temp}.\n"
                f"Last recorded pair: pressure "
                f"{last_pressure}, temperature {last_temp}.\n\n"
                f"The complete measurement data is available "
                f"to the visualization layer."
            )

        if variable == "salinity":

            count = summary.get(
                "salinity_count",
                0
            )

            if count == 0:
                return (
                    f"No salinity measurements were available "
                    f"for WMO {wmo}."
                )

            minimum = summary.get(
                "salinity_min",
                "N/A"
            )

            maximum = summary.get(
                "salinity_max",
                "N/A"
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{count} salinity measurements.\n\n"
                f"Salinity range: {minimum} to {maximum}."
            )

        if variable == "pressure":

            count = summary.get(
                "pressure_count",
                0
            )

            if count == 0:
                return (
                    f"No pressure measurements were available "
                    f"for WMO {wmo}."
                )

            minimum = summary.get(
                "pressure_min",
                "N/A"
            )

            maximum = summary.get(
                "pressure_max",
                "N/A"
            )

            return (
                f"Float WMO {wmo}, Cycle {cycle}, "
                f"profile time {profile_time}, contains "
                f"{count} pressure measurements.\n\n"
                f"Pressure range: {minimum} to {maximum}."
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

            if summary.get("temperature_count", 0) > 0:
                context_lines.append(
                    f"Temperature measurements: {summary['temperature_count']} "
                    f"(unit: degrees Celsius)"
                )
                context_lines.append(
                    f"Temperature range: {summary['temperature_min']} to "
                    f"{summary['temperature_max']} degrees Celsius"
                )

            if summary.get("salinity_count", 0) > 0:
                context_lines.append(
                    f"Salinity measurements: {summary['salinity_count']} "
                    f"(unit: PSU)"
                )
                context_lines.append(
                    f"Salinity range: {summary['salinity_min']} to "
                    f"{summary['salinity_max']} PSU"
                )

            if summary.get("pressure_count", 0) > 0:
                context_lines.append(
                    f"Pressure measurements: {summary['pressure_count']} "
                    f"(unit: dbar)"
                )
                context_lines.append(
                    f"Pressure range: {summary['pressure_min']} to "
                    f"{summary['pressure_max']} dbar"
                )
            context_lines.append("")

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

10. Do not extrapolate beyond the supplied information.

11. Keep the answer concise and directly answer the question.

12. The numerical values in the context were calculated or
    retrieved by the application. Do not modify them.

RETRIEVED FACTUAL CONTEXT:

{compact_context}

USER QUESTION:

{question}

ANSWER:
"""

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

            wmo_number = decision.wmo_number

            logging.info(
                f"Detected WMO number: {wmo_number}"
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

            context_data = self._retrieve_semantic(
                question
            )

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
