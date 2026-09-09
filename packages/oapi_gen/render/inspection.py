"""Read shared type, multipart, and security information from the IR."""

from __future__ import annotations

from collections.abc import Iterator

from ..ir import (
    ApiSpec,
    ImportRef,
    Operation,
    SecurityScheme,
    TypeRef,
)


def all_type_refs(spec: ApiSpec) -> Iterator[TypeRef]:
    for operation in spec.operations:
        yield from operation.type_refs()


def imports_for_types(types: list[TypeRef]) -> set[ImportRef]:
    return {item for type_ref in types for item in type_ref.required_imports}


def models_for_types(types: list[TypeRef]) -> set[str]:
    return {name for type_ref in types for name in type_ref.model_names}


def used_security_schemes(spec: ApiSpec) -> list[SecurityScheme]:
    names = {
        item.scheme_name
        for operation in spec.operations
        for requirement in operation.security
        for item in requirement.schemes
    }
    return [scheme for scheme in spec.security_schemes if scheme.wire_name in names]


def has_multipart_files(spec: ApiSpec) -> bool:
    return any(
        field.is_file
        for operation in spec.operations
        if operation.request_body is not None
        for field in operation.request_body.multipart_fields
    )


def has_cookie_arrays(spec: ApiSpec) -> bool:
    return any(
        header.is_cookie_array
        for operation in spec.operations
        for response in operation.responses
        for header in response.headers
    )


def operation_security_schemes(
    operation: Operation,
    security_schemes: dict[str, SecurityScheme],
) -> list[SecurityScheme]:
    names = {item.scheme_name for requirement in operation.security for item in requirement.schemes}
    return [scheme for name, scheme in security_schemes.items() if name in names]


def operation_uses_security(operation: Operation) -> bool:
    return any(requirement.schemes for requirement in operation.security)
