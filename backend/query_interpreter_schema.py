"""JSON Schema for constrained Qwen3 generation (Phase 4A-F3, Experiment B).

The schema is derived from the existing QuerySpec enums, so it cannot introduce
a field or an enum value the project does not already support. It mirrors the
whitelist enforced by ``query_interpreter.decode_qwen_output`` but remains a
*generation* aid only: the strict decoder and ``validate_spec`` stay
authoritative, and nothing here repairs a response.

Cross-field rules that JSON Schema cannot express cheaply (a clarification must
not carry query fields, ``clarify`` requires a clarification string, an
unsupported result carries only its reason) are deliberately left to the
decoder. The schema therefore removes structural errors, not semantic ones.
"""

from __future__ import annotations

from query_spec import (
    Aggregation,
    DataMode,
    GroupBy,
    Intent,
    MeasurementValues,
    Parameter,
    QcScope,
    SortDirection,
    SortField,
)


def _enum(enum_type) -> dict:
    return {"enum": [member.value for member in enum_type]}


def _number_pair() -> dict:
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
    }


def qwen_response_schema() -> dict:
    """Return the JSON Schema used for schema-constrained generation."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent"],
        "properties": {
            "intent": _enum(Intent),
            "parameters": {
                "type": "array",
                "items": _enum(Parameter),
            },
            "wmo_numbers": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "cycle_numbers": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "time_range": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
            },
            "bounding_box": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "min_latitude": {"type": "number"},
                    "max_latitude": {"type": "number"},
                    "min_longitude": {"type": "number"},
                    "max_longitude": {"type": "number"},
                },
            },
            "pressure_range": _number_pair(),
            "depth_range_m": _number_pair(),
            "depth_requested": {"type": "boolean"},
            "values": _enum(MeasurementValues),
            "qc_filter": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scope", "flags"],
                "properties": {
                    "scope": _enum(QcScope),
                    "flags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "data_mode": _enum(DataMode),
            "aggregations": {
                "type": "array",
                "items": _enum(Aggregation),
            },
            "group_by": _enum(GroupBy),
            "sort": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": _enum(SortField),
                    "direction": _enum(SortDirection),
                },
            },
            "limit": {"type": "integer"},
            "semantic_query": {"type": "string"},
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "clarification": {"type": "string"},
            "unsupported_reason": {"type": "string"},
        },
    }
