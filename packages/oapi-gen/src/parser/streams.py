"""Parse the schema-defined fields of a server-sent event."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import GenerationError
from ..ir import EventField
from .references import Resolver
from .schemas import SchemaParser, resolve_schema, schema_constraints
from .serialization import _JSON_MEDIA_TYPE
from .values import array_value, object_value, optional_string

STREAM_MEDIA_TYPES = frozenset(
    {"text/event-stream", "application/jsonl", "application/x-ndjson", "application/json-seq"}
)


def parse_event_fields(
    resolver: Resolver, schemas: SchemaParser, schema: Mapping[str, Any], context: str
) -> tuple[EventField, ...]:
    raw = resolve_schema(schema, resolver, context)
    schema_constraints(raw, context)
    if raw.get("type") != "object" or any(
        key in raw for key in ("allOf", "anyOf", "oneOf", "const", "enum", "discriminator")
    ):
        raise GenerationError(f"{context}: SSE itemSchema must be a flat object schema")
    properties = object_value(raw.get("properties"), f"{context}.properties")
    required = array_value(raw.get("required", []), f"{context}.required")
    if not all(isinstance(name, str) and name in properties for name in required):
        raise GenerationError(f"{context}.required must contain declared property names")
    if "data" not in properties or "data" not in required:
        raise GenerationError(f"{context}: SSE itemSchema must declare and require data")
    if set(properties) - {"data", "event", "id", "retry"}:
        raise GenerationError(f"{context}: SSE supports only data, event, id and retry fields")
    if isinstance(raw.get("additionalProperties"), dict):
        raise GenerationError(f"{context}: SSE additionalProperties schemas are not supported")

    fields: list[EventField] = []
    for name in ("data", "event", "id", "retry"):
        if name not in properties:
            continue
        field_context = f"{context}.properties.{name}"
        field_schema = resolve_schema(properties[name], resolver, field_context)
        if any(key in field_schema for key in ("allOf", "anyOf", "oneOf")):
            raise GenerationError(f"{field_context}: composed SSE field schemas are not supported")
        expected_type = "integer" if name == "retry" else "string"
        if field_schema.get("type") != expected_type or field_schema.get("nullable") is True:
            raise GenerationError(f"{field_context}: SSE {name} must have type {expected_type}")
        if "contentEncoding" in field_schema:
            raise GenerationError(f"{field_context}: SSE contentEncoding is not supported")
        content_type = optional_string(
            field_schema.get("contentMediaType"), f"{field_context}.contentMediaType"
        )
        json_encoded = content_type is not None
        if json_encoded and (name != "data" or not _JSON_MEDIA_TYPE.fullmatch(content_type)):
            raise GenerationError(
                f"{field_context}: only JSON contentMediaType on data is supported"
            )
        if "contentSchema" in field_schema and not json_encoded:
            raise GenerationError(
                f"{field_context}: contentSchema requires a JSON contentMediaType"
            )
        wire_type = schemas.parse(field_schema, field_context)
        application_schema = (
            object_value(field_schema.get("contentSchema", {}), f"{field_context}.contentSchema")
            if json_encoded
            else field_schema
        )
        fields.append(
            EventField(
                python_name=name,
                type_ref=schemas.parse(application_schema, field_context),
                wire_type_ref=wire_type,
                required=name in required,
                description=optional_string(field_schema.get("description"), field_context),
                json_encoded=json_encoded,
                property_counts=schemas.property_counts(application_schema, field_context),
            )
        )
    return tuple(fields)
