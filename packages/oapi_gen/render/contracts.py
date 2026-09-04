"""Render framework-independent request, response, and handler contracts."""

from __future__ import annotations

from collections.abc import Iterable

from ..ir import ApiSpec, MultipartField, Parameter, RequestBody, ResponseHeader
from .inspection import (
    all_type_refs,
    has_cookie_arrays,
    has_multipart_files,
    imports_for_types,
    models_for_types,
    multipart_class_name,
    operation_uses_security,
    type_annotation,
    used_security_schemes,
)
from .writer import Writer, generated_header, render_imports, render_model_imports


def render_contracts(spec: ApiSpec) -> str:
    writer = Writer()
    writer.line(generated_header(spec.source_hash))
    writer.line("from __future__ import annotations")

    type_refs = list(all_type_refs(spec))
    imports = imports_for_types(type_refs)
    imports.add(("dataclasses", "dataclass"))
    imports.add(("typing", "Protocol"))
    render_imports(writer, imports)
    render_model_imports(writer, models_for_types(type_refs))
    if has_cookie_arrays(spec):
        writer.require("._cookies", "Cookie as Cookie")

    if has_multipart_files(spec):
        writer.line()
        writer.line()
        writer.line("class MultipartFile(Protocol):")
        writer.line("filename: str | None", indent=1)
        writer.line("content_type: str | None", indent=1)
        writer.line("size: int | None", indent=1)
        writer.line()
        writer.line("async def read(self, size: int = -1) -> bytes: ...", indent=1)

    security_schemes = used_security_schemes(spec)
    for scheme in security_schemes:
        writer.line()
        writer.line()
        writer.line("@dataclass(frozen=True, slots=True)")
        writer.line(f"class {scheme.class_name}:")
        if scheme.scheme_type == "apiKey":
            writer.line("api_key: str", indent=1)
            writer.line("roles: tuple[str, ...]", indent=1)
        elif scheme.scheme_type == "basic":
            writer.line("username: str", indent=1)
            writer.line("password: str", indent=1)
            writer.line("roles: tuple[str, ...]", indent=1)
        elif scheme.scheme_type == "bearer":
            writer.line("token: str", indent=1)
            writer.line("roles: tuple[str, ...]", indent=1)
        else:
            writer.line("token: str", indent=1)
            writer.line("scopes: tuple[str, ...]", indent=1)

    if security_schemes:
        writer.line()
        writer.line()
        writer.line("class SecurityRejected(Exception):")
        writer.line('"""Signal that one security alternative was not accepted."""', indent=1)
        writer.line()
        writer.line()
        writer.line("class SecurityHandler(Protocol):")
        for scheme in security_schemes:
            writer.line(
                f"async def handle_{scheme.python_name}(self, context: object | None, "
                f"operation_id: str, credential: {scheme.class_name}) -> object | None: ...",
                indent=1,
            )

    for operation in spec.operations:
        if operation.request_body is not None and operation.request_body.is_multipart:
            body = operation.request_body
            writer.line()
            writer.line()
            writer.line("@dataclass(frozen=True, slots=True, kw_only=True)")
            writer.line(f"class {multipart_class_name(body)}:")
            render_docstring(writer, body.description, body.multipart_fields)
            for field in body.multipart_fields:
                default = "" if field.required else " = None"
                field_type = field.type_ref if field.required else field.type_ref.optional()
                writer.line(
                    f"{field.python_name}: {type_annotation(field_type)}{default}",
                    indent=1,
                )

        writer.line()
        writer.line()
        writer.line(f"class {operation.class_name}:")
        with writer.indented():
            writer.line("@dataclass(frozen=True, slots=True, kw_only=True)")
            writer.line("class Request:")
            described_fields: list[Parameter | RequestBody] = [*operation.parameters]
            if operation.request_body is not None:
                described_fields.append(operation.request_body)
            render_docstring(writer, operation.description or operation.summary, described_fields)
            fields = [*operation.parameters]
            if fields:
                for parameter in fields:
                    writer.line(
                        f"{parameter.python_name}: {type_annotation(parameter.type_ref)}", indent=1
                    )
            if operation.request_body is not None:
                body = operation.request_body
                body_type = body.type_ref if body.required else body.type_ref.optional()
                writer.line(f"{body.python_name}: {type_annotation(body_type)}", indent=1)
            if operation_uses_security(operation):
                writer.line("security_context: object | None", indent=1)
            if (
                not fields
                and operation.request_body is None
                and not operation_uses_security(operation)
            ):
                writer.line("pass", indent=1)

            for response in operation.responses:
                writer.line()
                writer.line()
                if response.status_code >= 400:
                    # Exceptions need writable traceback attributes for context managers.
                    writer.line("@dataclass(slots=True)")
                    writer.line(f"class {response.class_name}(Exception):")
                else:
                    writer.line("@dataclass(frozen=True, slots=True)")
                    writer.line(f"class {response.class_name}:")
                render_docstring(writer, response.description, response.headers)
                if response.type_ref is None and not response.headers:
                    writer.line("pass", indent=1)
                else:
                    if response.type_ref is not None:
                        writer.line(f"body: {type_annotation(response.type_ref)}", indent=1)
                    for header in response.headers:
                        if header.required:
                            writer.line(
                                f"{header.python_name}: {response_header_annotation(header)}",
                                indent=1,
                            )
                    for header in response.headers:
                        if not header.required:
                            annotation = response_header_annotation(header)
                            writer.line(
                                f"{header.python_name}: {annotation} = None",
                                indent=1,
                            )

            union = " | ".join(
                response.class_name
                for response in operation.responses
                if response.status_code < 400
            )
            if not union:
                writer.require("typing", "Never")
                union = "Never"
            writer.line()
            writer.line(f"type Response = {union}")

    for group in spec.groups:
        writer.line()
        writer.line()
        writer.line(f"class {group.class_name}(Protocol):")
        for operation in group.operations:
            writer.line(
                f"async def {operation.python_name}(self, request: {operation.class_name}.Request) "
                f"-> {operation.class_name}.Response:",
                indent=1,
            )
            description = "\n\n".join(
                text for text in (operation.summary, operation.description) if text
            )
            if operation.deprecated:
                description = (description + "\n\nDeprecated.").strip()
            if description:
                writer.line(repr(description), indent=2)
            writer.line("...", indent=2)

    writer.line()
    writer.line()
    writer.line("@dataclass(frozen=True, slots=True, kw_only=True)")
    writer.line("class Handlers:")
    if spec.groups:
        for group in spec.groups:
            writer.line(f"{group.field_name}: {group.class_name}", indent=1)
    else:
        writer.line("pass", indent=1)
    return writer.render()


def response_header_annotation(header: ResponseHeader) -> str:
    if header.is_cookie_array:
        return "list[Cookie] | None" if header.type_ref.nullable else "list[Cookie]"
    return type_annotation(header.type_ref)


def render_docstring(
    writer: Writer,
    description: str | None,
    fields: Iterable[Parameter | RequestBody | MultipartField | ResponseHeader],
) -> None:
    parts = [description] if description else []
    parts.extend(
        f"{field.python_name}: {field.description}" for field in fields if field.description
    )
    if parts:
        writer.line(repr("\n\n".join(parts)), indent=1)


def render_contract_imports(writer: Writer, spec: ApiSpec) -> None:
    names = {"Handlers"}
    security_schemes = used_security_schemes(spec)
    if security_schemes:
        names.add("SecurityHandler")
        names.add("SecurityRejected")
        names.update(scheme.class_name for scheme in security_schemes)
    for operation in spec.operations:
        writer.require(".contracts", f"{operation.class_name} as _contracts_{operation.class_name}")
        if operation.request_body is not None and operation.request_body.is_multipart:
            names.add(multipart_class_name(operation.request_body))
    if has_cookie_arrays(spec):
        writer.require(".contracts", "Cookie as _contracts_Cookie")
    for name in names:
        writer.require(".contracts", name)
