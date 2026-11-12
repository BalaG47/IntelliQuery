"""Deterministic routing for ARGO questions.

The router deliberately makes only decisions that the backend can execute:
complete enumerations and counts, a named float (MySQL), or similarity-based
profile discovery (ChromaDB).  Keeping this separate from answer generation
prevents small wording changes from accidentally changing the data source.
"""

from dataclasses import dataclass
from enum import Enum
import re


class Route(str, Enum):
    """The supported retrieval paths."""

    MYSQL_PROFILE_COUNTS = "mysql_profile_counts"
    MYSQL_WMO_PROFILE_COUNT = "mysql_wmo_profile_count"
    MYSQL_FLOAT_ENUMERATION = "mysql_float_enumeration"
    MYSQL_WMO = "mysql_wmo"
    MYSQL_REGIONAL_TREND = "mysql_regional_trend"
    GENERAL_CONVERSATION = "general_conversation"
    CHROMA_SEMANTIC = "chromadb_semantic"


@dataclass(frozen=True)
class QueryRoute:
    """A retrieval decision and the reason it was made."""

    route: Route
    reason: str
    wmo_number: int | None = None


class ArgoQueryRouter:
    """Classify a user question without calling an LLM.

    MySQL is used whenever the user asks for a complete, exact relational
    result. ChromaDB is used for semantic discovery, where a top-k result is
    appropriate. A seven-digit WMO is an unambiguous exact identifier and
    always takes priority over semantic matching.
    """

    _PROFILE_COUNT_PATTERNS = (
        r"\bhow many\s+profiles\b.*\b(each|per)\s+(float|wmo|id)",
        r"\b(number|count)s?\s+of\s+profiles\b.*\b(each|per)\s+(float|wmo|id)",
        r"\bprofile\s+counts?\b.*\b(each|per|by)\s+(float|wmo|id)",
    )

    _FLOAT_ENUMERATION_PATTERNS = (
        r"\b(list|show|give|display)\b.*\b(all|available)?\s*(float|wmo)\s*(ids?|numbers?)\b",
        r"\b(list|show|give|display)\b.*\b(all|available)\s+(float|wmo)s?\b",
        r"\b(which|what)\b.*\b(float|wmo)\s*(ids?|numbers?)\b.*\b(available|exist)\b",
        r"\b(which|what)\b.*\b(float|wmo)s?\b.*\b(available|exist)\b",
        r"\bhow many\s+(float|wmo)s?\b",
        r"\b(number|count)\s+of\s+(available\s+)?(float|wmo)s?\b",
    )

    @staticmethod
    def _is_general_conversation(question: str) -> bool:
        """Detect simple conversational messages."""

        normalized = " ".join(question.lower().strip().split())

        conversational_messages = {
            "hi",
            "hello",
            "hey",
            "hey there",
            "good morning",
            "good afternoon",
            "good evening",
            "how are you",
            "what's up",
            "whats up",
            "thanks",
            "thank you",
            "thank you so much",
            "bye",
            "goodbye",
        }

        normalized = re.sub(r"[!?.]+$", "", normalized)

        return normalized in conversational_messages

    @classmethod
    def route(cls, question: str) -> QueryRoute:
        normalized = " ".join(question.lower().split())
        # Handle ordinary conversation before database routing.
        if cls._is_general_conversation(normalized):
            return QueryRoute(
                Route.GENERAL_CONVERSATION,
                "The message is ordinary conversation and requires no data retrieval.",
            )

        wmo_match = re.search(r"\b\d{7}\b", normalized)

        if (
            wmo_match
            and re.search(r"\b(how many|count|number of)\b.*\bprofiles?\b", normalized)
        ):
            return QueryRoute(
                Route.MYSQL_WMO_PROFILE_COUNT,
                "A profile count for one WMO requires an exact MySQL count.",
                int(wmo_match.group()),
            )

        years = re.findall(r"\b(?:19|20)\d{2}\b", normalized)
        if (
            "arabian sea" in normalized
            and "trend" in normalized
            and any(variable in normalized for variable in ("salinity", "temperature"))
            and len(years) >= 2
        ):
            return QueryRoute(
                Route.MYSQL_REGIONAL_TREND,
                "A regional time trend needs exact MySQL records for the requested years.",
            )

        if any(re.search(pattern, normalized) for pattern in cls._PROFILE_COUNT_PATTERNS):
            return QueryRoute(
                Route.MYSQL_PROFILE_COUNTS,
                "Profile counts require a complete relational aggregation.",
            )

        if any(re.search(pattern, normalized) for pattern in cls._FLOAT_ENUMERATION_PATTERNS):
            return QueryRoute(
                Route.MYSQL_FLOAT_ENUMERATION,
                "Float enumeration requires the complete relational result set.",
            )

        if wmo_match:
            return QueryRoute(
                Route.MYSQL_WMO,
                "A WMO number is an exact MySQL identifier.",
                int(wmo_match.group()),
            )

        return QueryRoute(
            Route.CHROMA_SEMANTIC,
            "The question asks for semantic profile discovery.",
        )
