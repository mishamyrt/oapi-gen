"""Parse fixed response statuses, bodies, and response headers."""

from __future__ import annotations

from http import HTTPStatus

from ..errors import GenerationError
from ..ir import (
    Response,
    ResponseHeader,
    TypeRef,
)
from ..naming import pascal_case, snake_case
from .references import Resolver
from .schemas import SchemaParser, resolve_schema
from .serialization import _JSON_MEDIA_TYPE, validate_parameter_serialization
from .streams import STREAM_MEDIA_TYPES, parse_event_fields
from .values import object_value, optional_string, required_string


def parse_responses(
    resolver: Resolver,
    schemas: SchemaParser,
    value: object,
    context: str,
    *,
    openapi_version: str = "3.1.0",
) -> tuple[Response, ...]:
    raw_responses = object_value(value, f"{context}.responses")
    if not raw_responses:
        raise GenerationError(f"{context}: at least one response is required")

    responses: list[Response] = []
    for raw_status_code, value in raw_responses.items():
        status_text = str(raw_status_code)
        if not status_text.isdigit() or not 100 <= int(status_text) <= 599:
            raise GenerationError(
                f"{context}: only fixed numeric response status codes are supported, "
                f"got {status_text!r}"
            )
        status_code = int(status_text)
        raw = resolver.resolve_object(value, f"{context}.responses.{status_text}")
        headers = _parse_response_headers(
            resolver,
            schemas,
            raw.get("headers", {}),
            f"{context}.responses.{status_text}.headers",
            is_error=status_code >= 400,
        )
        response_context = f"{context}.responses.{status_text}"
        description = (
            optional_string(raw.get("description"), f"{response_context}.description")
            if openapi_version.startswith("3.2.")
            else required_string(raw, "description", response_context)
        )
        summary = optional_string(raw.get("summary"), f"{response_context}.summary")
        content = object_value(raw.get("content", {}), f"{context}.responses.{status_text}.content")
        type_ref: TypeRef | None = None
        media_type: str | None = None
        property_counts = None
        streaming = False
        event_fields = ()
        if content:
            if len(content) != 1:
                raise GenerationError(
                    f"{context}: exactly one response media type is supported for {status_text}"
                )
            media_type, media = next(iter(content.items()))
            streaming = media_type in STREAM_MEDIA_TYPES
            if not (_JSON_MEDIA_TYPE.fullmatch(media_type) or streaming):
                raise GenerationError(f"{context}: unsupported response media type {media_type!r}")
            media_object = resolver.resolve_object(media, f"{response_context}.{media_type}")
            for keyword in ("itemEncoding", "prefixEncoding"):
                if keyword in media_object:
                    raise GenerationError(f"{response_context}: {keyword} is not supported yet")
            if streaming:
                if not openapi_version.startswith("3.2."):
                    raise GenerationError(
                        f"{response_context}: itemSchema streams require OpenAPI 3.2"
                    )
                if status_code < 200 or status_code in {204, 205, 304}:
                    raise GenerationError(f"{response_context}: this status cannot carry a stream")
                if any(header.wire_name.lower() == "content-length" for header in headers):
                    raise GenerationError(
                        f"{response_context}: streams cannot declare Content-Length"
                    )
                if "schema" in media_object:
                    raise GenerationError(
                        f"{response_context}: streaming responses use itemSchema; "
                        "whole-stream schema validation is not supported"
                    )
            elif "itemSchema" in media_object:
                raise GenerationError(
                    f"{response_context}: itemSchema requires a sequential media type"
                )
            schema_key = "itemSchema" if streaming else "schema"
            schema = object_value(media_object.get(schema_key), f"{response_context}.{schema_key}")
            if media_type == "text/event-stream":
                event_fields = parse_event_fields(resolver, schemas, schema, response_context)
            else:
                type_ref = schemas.parse(schema, response_context)
            property_counts = schemas.property_counts(schema, f"{context}.responses.{status_text}")

        responses.append(
            Response(
                status_code=status_code,
                class_name=response_class_name(status_code),
                type_ref=type_ref,
                description=description,
                media_type=media_type,
                headers=headers,
                property_counts=property_counts,
                summary=summary,
                streaming=streaming,
                event_fields=event_fields,
            )
        )
    return tuple(sorted(responses, key=lambda response: response.status_code))


def response_class_name(status_code: int) -> str:
    # Keep names stable across the HTTPStatus renames in Python 3.13.
    names = {
        413: "PayloadTooLarge",
        414: "UriTooLong",
        416: "RangeNotSatisfiable",
        422: "UnprocessableEntity",
    }
    if status_code in names:
        return names[status_code]
    try:
        return pascal_case(HTTPStatus(status_code).name)
    except ValueError:
        return f"Status{status_code}"


def _parse_response_headers(
    resolver: Resolver,
    schemas: SchemaParser,
    value: object,
    context: str,
    *,
    is_error: bool,
) -> tuple[ResponseHeader, ...]:
    raw_headers = object_value(value, context)
    normalized_names: set[str] = set()
    headers: list[ResponseHeader] = []
    for wire_name, value in raw_headers.items():
        if not isinstance(wire_name, str) or not wire_name:
            raise GenerationError(f"{context} keys must be non-empty strings")
        if wire_name.lower() == "content-type":
            raise GenerationError(f"{context}: Content-Type must be declared as response content")
        normalized = wire_name.lower()
        if normalized in normalized_names:
            raise GenerationError(f"{context}: duplicate response header {wire_name!r}")
        normalized_names.add(normalized)

        header_context = f"{context}.{wire_name}"
        raw = resolver.resolve_object(value, header_context)
        if "content" in raw:
            raise GenerationError(f"{header_context}: header content is not supported yet")
        validate_parameter_serialization(raw, "header", wire_name, context)
        schema = object_value(raw.get("schema"), f"{header_context}.schema")
        type_ref = schemas.parse(schema, header_context)
        is_cookie_array = False
        if normalized == "set-cookie":
            resolved = resolve_schema(schema, resolver, header_context)
            raw_type = resolved.get("type")
            is_cookie_array = raw_type == "array" or (
                isinstance(raw_type, list) and set(raw_type) in ({"array"}, {"array", "null"})
            )
        required = bool(raw.get("required", False))
        if not required:
            type_ref = type_ref.optional()
        try:
            python_name = snake_case(wire_name)
        except ValueError as error:
            raise GenerationError(f"{header_context}: invalid header name") from error
        if python_name == "body" or (is_error and hasattr(Exception, python_name)):
            python_name += "_header"
        headers.append(
            ResponseHeader(
                wire_name=wire_name,
                python_name=python_name,
                type_ref=type_ref,
                required=required,
                description=optional_string(
                    raw.get("description"), f"{header_context}.description"
                ),
                schema=dict(schema),
                is_cookie_array=is_cookie_array,
            )
        )

    python_names = [header.python_name for header in headers]
    if len(python_names) != len(set(python_names)):
        raise GenerationError(f"{context}: response header names collide after normalization")
    return tuple(headers)
