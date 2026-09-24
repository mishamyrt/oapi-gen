"""Load OpenAPI documents and coordinate validation and operation parsing."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from ..errors import GenerationError
from ..ir import ApiSpec, Operation
from ..naming import pascal_case
from .operations import (
    assign_operation_names,
    make_groups,
    parse_operation,
    validate_operation_names,
)
from .references import Resolver
from .schemas import SchemaParser, prepare_component_schemas
from .security import parse_security_schemes
from .values import array_value, object_value, required_string

_HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")


def load_document(path: Path) -> dict[str, Any]:
    try:
        source = path.read_bytes()
    except OSError as error:
        raise GenerationError(f"cannot read {path}: {error}") from error

    try:
        value = json.loads(source) if path.suffix.lower() == ".json" else yaml.safe_load(source)
    except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError) as error:
        raise GenerationError(f"cannot parse {path}: {error}") from error

    if not isinstance(value, dict):
        raise GenerationError("the OpenAPI document root must be an object")
    return cast(dict[str, Any], value)


def _validate_component_names(document: Mapping[str, Any]) -> None:
    components = object_value(document.get("components", {}), "components")
    schemas = object_value(components.get("schemas", {}), "components.schemas")
    if not all(isinstance(name, str) and name for name in schemas):
        raise GenerationError("components.schemas keys must be non-empty strings")
    try:
        generated_names = [pascal_case(cast(str, name)) for name in schemas]
    except ValueError as error:
        raise GenerationError("components.schemas contains an invalid Python name") from error
    duplicates = sorted(name for name, count in Counter(generated_names).items() if count > 1)
    if duplicates:
        raise GenerationError(
            "component schema names collide after Python name normalization: "
            + ", ".join(duplicates)
        )


class OpenAPIParser:
    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._resolver = Resolver(document)

    def parse(self) -> ApiSpec:
        openapi_version = required_string(self._document, "openapi", "document")
        if not openapi_version.startswith(("3.0.", "3.1.", "3.2.")):
            raise GenerationError(
                f"unsupported OpenAPI version {openapi_version!r}; "
                "expected OpenAPI 3.0.x, 3.1.x or 3.2.x"
            )
        if self._document.get("webhooks"):
            raise GenerationError("webhooks are not supported yet")

        info = object_value(self._document.get("info"), "info")
        title = required_string(info, "title", "info")
        api_version = required_string(info, "version", "info")
        _validate_component_names(self._document)
        prepare_component_schemas(self._document, self._resolver)
        schemas = SchemaParser(self._resolver)
        security_schemes = parse_security_schemes(self._document, self._resolver)
        security_schemes_by_name = {scheme.wire_name: scheme for scheme in security_schemes}
        paths = object_value(self._document.get("paths"), "paths")
        global_security = self._document.get("security")

        operations: list[Operation] = []
        for path in sorted(paths):
            if not path.startswith("/"):
                raise GenerationError(f"path {path!r} must start with '/'")
            path_item = self._resolver.resolve_object(paths[path], f"paths.{path}")
            for keyword in ("query", "additionalOperations"):
                if keyword in path_item:
                    raise GenerationError(f"paths.{path}: {keyword} is not supported yet")
            path_parameters = array_value(
                path_item.get("parameters", []), f"paths.{path}.parameters"
            )
            for method in _HTTP_METHODS:
                raw_operation = path_item.get(method)
                if raw_operation is None:
                    continue
                operation_object = self._resolver.resolve_object(
                    raw_operation, f"paths.{path}.{method}"
                )
                operations.append(
                    parse_operation(
                        self._resolver,
                        schemas,
                        method,
                        path,
                        operation_object,
                        path_parameters,
                        global_security,
                        security_schemes_by_name,
                        openapi_version=openapi_version,
                    )
                )

        validate_operation_names(operations)
        operations = assign_operation_names(operations, security_schemes)
        groups = make_groups(operations)
        return ApiSpec(
            title=title,
            api_version=api_version,
            operations=tuple(operations),
            groups=groups,
            security_schemes=security_schemes,
        )


def parse_openapi(path: Path) -> tuple[ApiSpec, dict[str, Any]]:
    document = load_document(path)
    return OpenAPIParser(document).parse(), document
