"""Merge operation parameters and validate their wire representation."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, cast

from ..errors import GenerationError
from ..ir import (
    Parameter,
    ParameterLocation,
)
from ..naming import available_name, snake_case
from .references import Resolver
from .schemas import SchemaParser, resolve_schema
from .serialization import validate_parameter_serialization
from .values import array_value, object_value, optional_string, required_string

_PATH_PARAMETER = re.compile(r"{([^{}]+)}")
_PARAMETER_LOCATIONS = {"path", "query", "header", "cookie"}


def parse_parameters(
    resolver: Resolver,
    schemas: SchemaParser,
    path: str,
    raw_parameters: list[object],
    context: str,
) -> tuple[Parameter, ...]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for index, value in enumerate(raw_parameters):
        raw = resolver.resolve_object(value, f"{context}.parameters[{index}]")
        wire_name = required_string(raw, "name", f"{context}.parameters[{index}]")
        location_value = required_string(raw, "in", f"{context}.parameters[{index}]")
        if location_value not in _PARAMETER_LOCATIONS:
            raise GenerationError(
                f"{context}: unsupported parameter location {location_value!r} for {wire_name!r}"
            )
        location = cast(ParameterLocation, location_value)
        duplicate_key = (wire_name.lower() if location == "header" else wire_name, location)
        merged[duplicate_key] = raw

    try:
        base_names = [snake_case(required_string(raw, "name", context)) for raw in merged.values()]
    except ValueError as error:
        raise GenerationError(f"{context}: invalid parameter name") from error
    name_counts = Counter(base_names)
    used_names: set[str] = set()
    parameters: list[Parameter] = []
    for raw, base_name in zip(merged.values(), base_names, strict=True):
        wire_name = required_string(raw, "name", context)
        location = cast(ParameterLocation, required_string(raw, "in", context))
        preferred_name = f"{base_name}_{location}" if name_counts[base_name] > 1 else base_name
        python_name = available_name(preferred_name, used_names)
        required = location == "path" or bool(raw.get("required", False))
        schema_context = f"{context}.parameters.{wire_name}.schema"
        schema = object_value(raw.get("schema"), schema_context)
        resolved_schema = resolve_schema(schema, resolver, schema_context)
        validate_parameter_serialization(raw, location, wire_name, context)
        wire_schema = _parameter_wire_schema(resolver, schema, schema_context)
        raw_type = wire_schema.get("type")
        is_array = raw_type == "array" or (isinstance(raw_type, list) and "array" in raw_type)
        if is_array and location == "cookie":
            raise GenerationError(
                f"{context}: array cookie parameter {wire_name!r} is not supported"
            )
        type_ref = schemas.parse(schema, f"{context}.parameters.{wire_name}")
        has_default = "default" in resolved_schema
        default = resolved_schema.get("default")
        if has_default and isinstance(default, (dict, list)):
            raise GenerationError(
                f"{context}: mutable default for parameter {wire_name!r} is not supported"
            )
        if (not required and not has_default) or (has_default and default is None):
            type_ref = type_ref.optional()

        parameters.append(
            Parameter(
                wire_name=wire_name,
                python_name=python_name,
                location=location,
                type_ref=type_ref,
                required=required,
                description=optional_string(
                    raw.get("description"), f"{context}.parameters.{wire_name}.description"
                ),
                default=default,
                has_default=has_default,
                wire_schema=wire_schema,
            )
        )

    placeholders = set(_PATH_PARAMETER.findall(path))
    path_parameters = {
        parameter.wire_name for parameter in parameters if parameter.location == "path"
    }
    if placeholders != path_parameters:
        raise GenerationError(
            f"{context}: path placeholders {sorted(placeholders)!r} do not match "
            f"path parameters {sorted(path_parameters)!r}"
        )
    return tuple(parameters)


def _parameter_wire_schema(
    resolver: Resolver,
    schema: object,
    context: str,
    *,
    allow_array: bool = True,
    resolving_refs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    value = object_value(schema, context)
    ref = value.get("$ref")
    if isinstance(ref, str):
        if ref in resolving_refs:
            raise GenerationError(f"{context}: recursive parameter schemas are not supported")
        resolving_refs = resolving_refs | {ref}
    raw = resolve_schema(schema, resolver, context)
    raw_type = raw.get("type")
    if not (
        raw_type is None
        or isinstance(raw_type, str)
        or (isinstance(raw_type, list) and all(isinstance(item, str) for item in raw_type))
    ):
        raise GenerationError(f"{context}: schema type must be a string or an array of strings")
    types = set(raw_type) if isinstance(raw_type, list) else {raw_type}
    if types - {"null"} == {"array"} and allow_array:
        if any(keyword in raw for keyword in ("oneOf", "anyOf", "allOf")):
            raise GenerationError(f"{context}: composed array parameters are not supported")
        raw["items"] = _parameter_wire_schema(
            resolver,
            raw.get("items"),
            f"{context}.items",
            allow_array=False,
            resolving_refs=resolving_refs,
        )
        return raw
    for keyword in ("oneOf", "anyOf", "allOf"):
        if keyword in raw:
            raw[keyword] = [
                _parameter_wire_schema(
                    resolver,
                    member,
                    f"{context}.{keyword}[{index}]",
                    allow_array=False,
                    resolving_refs=resolving_refs,
                )
                for index, member in enumerate(array_value(raw[keyword], context))
            ]
            return raw
    if types <= {"string", "integer", "number", "boolean", "null"}:
        return raw
    values = raw.get("enum", [raw["const"]] if "const" in raw else [])
    if (
        types == {None}
        and isinstance(values, list)
        and values
        and all(isinstance(value, (str, int, float, bool)) or value is None for value in values)
    ):
        return raw
    raise GenerationError(
        f"{context}: parameter schemas must be scalars or one-dimensional arrays of scalars"
    )
