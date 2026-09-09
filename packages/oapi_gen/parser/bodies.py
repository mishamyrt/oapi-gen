"""Parse JSON and multipart request bodies, including form fields and uploads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..errors import GenerationError
from ..ir import (
    MultipartField,
    RequestBody,
    TypeRef,
)
from ..naming import snake_case
from .references import Resolver
from .schemas import SchemaParser, resolve_schema, schema_constraints
from .serialization import _JSON_MEDIA_TYPE
from .values import array_value, object_value, optional_string


def parse_request_body(
    resolver: Resolver,
    schemas: SchemaParser,
    value: object,
    python_name: str,
    multipart_class_name: str,
    context: str,
) -> RequestBody | None:
    if value is None:
        return None
    raw = resolver.resolve_object(value, f"{context}.requestBody")
    content = object_value(raw.get("content"), f"{context}.requestBody.content")
    if len(content) != 1:
        raise GenerationError(f"{context}: exactly one request body media type is supported")
    media_type, media = next(iter(content.items()))
    if not (_JSON_MEDIA_TYPE.match(media_type) or media_type == "multipart/form-data"):
        raise GenerationError(f"{context}: unsupported request media type {media_type!r}")
    media_object = resolver.resolve_object(media, f"{context}.requestBody.content.{media_type}")
    for keyword in ("itemSchema", "itemEncoding", "prefixEncoding"):
        if keyword in media_object:
            raise GenerationError(f"{context}.requestBody: {keyword} is not supported yet")
    schema = object_value(media_object.get("schema"), f"{context}.requestBody.schema")
    required = bool(raw.get("required", False))
    multipart_fields: tuple[MultipartField, ...] = ()
    if media_type == "multipart/form-data":
        multipart_fields = _parse_multipart_fields(
            resolver,
            schemas,
            schema,
            media_object.get("encoding", {}),
            context,
        )
        type_ref = TypeRef(multipart_class_name)
    else:
        type_ref = schemas.parse(schema, f"{context}.requestBody")
    return RequestBody(
        python_name=python_name,
        type_ref=type_ref,
        required=required,
        description=optional_string(raw.get("description"), f"{context}.requestBody.description"),
        media_type=media_type,
        multipart_fields=multipart_fields,
        property_counts=schemas.property_counts(schema, f"{context}.requestBody.schema"),
    )


def _parse_multipart_fields(
    resolver: Resolver,
    schemas: SchemaParser,
    schema: Mapping[str, Any],
    encoding_value: object,
    context: str,
) -> tuple[MultipartField, ...]:
    raw_schema = resolve_schema(schema, resolver, f"{context}.requestBody.schema")
    schema_constraints(raw_schema, f"{context}.requestBody.schema")
    if raw_schema.get("type") not in (None, "object"):
        raise GenerationError(f"{context}: multipart request schema must be an object")
    properties = object_value(
        raw_schema.get("properties"), f"{context}.requestBody.schema.properties"
    )
    if not all(isinstance(name, str) and name for name in properties):
        raise GenerationError(
            f"{context}.requestBody.schema.properties keys must be non-empty strings"
        )
    raw_required = array_value(
        raw_schema.get("required", []), f"{context}.requestBody.schema.required"
    )
    if not all(isinstance(name, str) for name in raw_required):
        raise GenerationError(f"{context}.requestBody.schema.required must contain strings")
    required_names = set(cast(list[str], raw_required))
    unknown_required = sorted(required_names - set(properties))
    if unknown_required:
        raise GenerationError(
            f"{context}.requestBody.schema.required contains unknown fields {unknown_required!r}"
        )
    encodings = object_value(encoding_value, f"{context}.requestBody.encoding")
    unknown_encodings = sorted(set(encodings) - set(properties))
    if unknown_encodings:
        raise GenerationError(
            f"{context}.requestBody.encoding contains unknown fields {unknown_encodings!r}"
        )

    try:
        base_names = [snake_case(cast(str, wire_name)) for wire_name in properties]
    except ValueError as error:
        raise GenerationError(f"{context}: invalid multipart field name") from error
    if len(base_names) != len(set(base_names)):
        raise GenerationError(f"{context}: multipart field names collide after normalization")

    fields: list[MultipartField] = []
    for (wire_name, value), python_name in zip(properties.items(), base_names, strict=True):
        field_context = f"{context}.requestBody.{wire_name}"
        raw_field = object_value(value, field_context)
        resolved_field = resolve_schema(raw_field, resolver, field_context)
        encoding = object_value(encodings.get(wire_name, {}), f"{field_context}.encoding")
        unsupported_encoding = sorted(
            set(encoding) - {"contentType", "style", "explode", "allowReserved", "headers"}
        )
        if unsupported_encoding:
            raise GenerationError(
                f"{field_context}.encoding contains unsupported fields {unsupported_encoding!r}"
            )
        if any(key in encoding for key in ("style", "explode", "allowReserved", "headers")):
            raise GenerationError(
                f"{field_context}: custom multipart serialization is not supported yet"
            )
        part_media_type = optional_string(
            encoding.get("contentType"), f"{field_context}.encoding.contentType"
        )

        is_array = resolved_field.get("type") == "array"
        item_schema = (
            resolve_schema(resolved_field.get("items"), resolver, f"{field_context}.items")
            if is_array
            else resolved_field
        )
        is_file = item_schema.get("type") == "string" and item_schema.get("format") == "binary"
        if is_file:
            annotation = "list[MultipartFile]" if is_array else "MultipartFile"
            constraints = schema_constraints(resolved_field, field_context)
            if (is_array and schema_constraints(item_schema, f"{field_context}.items")) or any(
                name not in {"min_length", "max_length"} or not is_array for name, _ in constraints
            ):
                raise GenerationError(
                    f"{field_context}: only minItems/maxItems constraints on file arrays "
                    "are supported"
                )
            type_ref = TypeRef(annotation).constrained(constraints)
        else:
            _validate_multipart_value_schema(resolver, resolved_field, field_context)
            type_ref = schemas.parse(raw_field, field_context)

        field_required = wire_name in required_names
        if not field_required and not is_file:
            type_ref = type_ref.optional()
        fields.append(
            MultipartField(
                wire_name=wire_name,
                python_name=python_name,
                type_ref=type_ref,
                required=field_required,
                description=optional_string(
                    resolved_field.get("description"), f"{field_context}.description"
                ),
                is_file=is_file,
                is_array=is_array,
                media_type=part_media_type,
            )
        )

    if not fields:
        raise GenerationError(f"{context}: multipart request schema must define properties")
    return tuple(fields)


def _validate_multipart_value_schema(
    resolver: Resolver,
    schema: Mapping[str, Any],
    context: str,
) -> None:
    for keyword in ("oneOf", "anyOf", "allOf"):
        if keyword in schema:
            members = array_value(schema[keyword], f"{context}.{keyword}")
            for index, member in enumerate(members):
                raw_member = object_value(member, f"{context}.{keyword}[{index}]")
                resolved = resolve_schema(raw_member, resolver, f"{context}.{keyword}[{index}]")
                _validate_multipart_value_schema(resolver, resolved, context)
            return
    if schema.get("type") == "array":
        items = resolve_schema(schema.get("items"), resolver, f"{context}.items")
        _validate_multipart_value_schema(resolver, items, f"{context}.items")
        return
    if schema.get("type") == "object" or any(
        key in schema for key in ("properties", "additionalProperties")
    ):
        raise GenerationError(f"{context}: complex multipart form fields are not supported yet")
