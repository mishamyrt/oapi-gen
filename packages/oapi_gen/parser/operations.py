"""Assemble operations and validate generated operation and handler group names."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from ..errors import GenerationError
from ..ir import (
    HandlerGroup,
    Operation,
    SecurityScheme,
)
from ..naming import available_name, pascal_case, snake_case
from .bodies import parse_request_body
from .parameters import parse_parameters
from .references import Resolver
from .responses import parse_responses
from .schemas import SchemaParser
from .security import parse_security_requirements
from .values import array_value, optional_string, required_string


def parse_operation(
    resolver: Resolver,
    schemas: SchemaParser,
    method: str,
    path: str,
    raw: Mapping[str, Any],
    path_parameters: list[object],
    global_security: object,
    security_schemes: Mapping[str, SecurityScheme],
) -> Operation:
    context = f"{method.upper()} {path}"
    operation_id = required_string(raw, "operationId", context)
    try:
        python_name = snake_case(operation_id)
        operation_class_name = pascal_case(operation_id)
    except ValueError as error:
        raise GenerationError(f"{context}: invalid operationId {operation_id!r}") from error

    if raw.get("callbacks"):
        raise GenerationError(f"{context}: callbacks are not supported yet")
    effective_security = raw.get("security", global_security)
    security = parse_security_requirements(
        effective_security,
        security_schemes,
        f"{context}.security",
    )

    tags_value = array_value(raw.get("tags", []), f"{context}.tags")
    if not all(isinstance(tag, str) for tag in tags_value):
        raise GenerationError(f"{context}.tags must contain strings")
    tags = tuple(cast(list[str], tags_value))
    raw_group = raw.get("x-handler-group", tags[0] if tags else "default")
    if not isinstance(raw_group, str) or not raw_group:
        raise GenerationError(f"{context}.x-handler-group must be a non-empty string")
    try:
        group_class_name = available_name(f"{pascal_case(raw_group)}Handler", {"SecurityHandler"})
        group_field_name = snake_case(raw_group)
    except ValueError as error:
        raise GenerationError(f"{context}: invalid handler group {raw_group!r}") from error

    operation_parameters = array_value(raw.get("parameters", []), f"{context}.parameters")
    parameters = parse_parameters(
        resolver,
        schemas,
        path,
        [*path_parameters, *operation_parameters],
        context,
    )
    used_field_names = {item.python_name for item in parameters}
    body_base_name = "body_payload" if "body" in used_field_names else "body"
    body_name = available_name(body_base_name, used_field_names)
    request_body = parse_request_body(
        resolver,
        schemas,
        raw.get("requestBody"),
        body_name,
        f"{operation_class_name}RequestMultipart",
        context,
    )
    if any(requirement.schemes for requirement in security) and any(
        parameter.python_name == "security_context" for parameter in parameters
    ):
        raise GenerationError(
            f"{context}: parameter name 'security_context' is reserved for secured operations"
        )
    responses = parse_responses(resolver, schemas, raw.get("responses"), context)

    return Operation(
        method=method,
        path=path,
        operation_id=operation_id,
        python_name=python_name,
        class_name=operation_class_name,
        group_name=raw_group,
        group_class_name=group_class_name,
        group_field_name=group_field_name,
        tags=tags,
        summary=optional_string(raw.get("summary"), f"{context}.summary"),
        description=optional_string(raw.get("description"), f"{context}.description"),
        deprecated=bool(raw.get("deprecated", False)),
        parameters=parameters,
        request_body=request_body,
        responses=responses,
        security=security,
    )


def validate_operation_names(operations: list[Operation]) -> None:
    for label, values in (
        ("operationId", [operation.operation_id for operation in operations]),
        ("Python operation name", [operation.python_name for operation in operations]),
        ("operation class name", [operation.class_name for operation in operations]),
    ):
        duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
        if duplicates:
            raise GenerationError(f"duplicate {label}: {', '.join(duplicates)}")


def assign_operation_names(
    operations: list[Operation], security_schemes: tuple[SecurityScheme, ...]
) -> list[Operation]:
    reserved = {
        "Cookie",
        "Exception",
        "Handlers",
        "MultipartFile",
        "Never",
        "SecurityHandler",
        "SecurityRejected",
        "Protocol",
    }
    reserved.update(operation.group_class_name for operation in operations)
    reserved.update(scheme.class_name for scheme in security_schemes)
    reserved.update(
        name
        for operation in operations
        for type_ref in operation.type_refs()
        for _, name in type_ref.required_imports
    )
    reserved.update(
        operation.request_body.type_ref.annotation
        for operation in operations
        if operation.request_body is not None and operation.request_body.is_multipart
    )
    used = reserved | {operation.class_name for operation in operations}
    return [
        replace(operation, class_name=available_name(f"{operation.class_name}Operation", used))
        if operation.class_name in reserved
        else operation
        for operation in operations
    ]


def make_groups(operations: list[Operation]) -> tuple[HandlerGroup, ...]:
    grouped: defaultdict[tuple[str, str, str], list[Operation]] = defaultdict(list)
    for operation in operations:
        grouped[
            operation.group_name,
            operation.group_class_name,
            operation.group_field_name,
        ].append(operation)

    class_names = [key[1] for key in grouped]
    field_names = [key[2] for key in grouped]
    if len(class_names) != len(set(class_names)) or len(field_names) != len(set(field_names)):
        raise GenerationError("handler group names collide after Python name normalization")
    return tuple(
        HandlerGroup(name, class_name, field_name, tuple(grouped[(name, class_name, field_name)]))
        for name, class_name, field_name in sorted(grouped)
    )
