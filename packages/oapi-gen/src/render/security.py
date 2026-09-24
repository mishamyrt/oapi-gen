"""Render ordered authorization alternatives for Starlette endpoints."""

from __future__ import annotations

from ..ir import (
    Operation,
    SecurityScheme,
)
from .inspection import operation_security_schemes
from .writer import Writer


def render_authorization(
    writer: Writer,
    operation: Operation,
    security_schemes: dict[str, SecurityScheme],
) -> None:
    writer.require("starlette.exceptions", "HTTPException")
    writer.line("__oapi_security_context: object | None = None", indent=2)
    writer.line("__oapi_security_satisfied = False", indent=2)
    names: dict[str, str] = {}
    for scheme in operation_security_schemes(operation, security_schemes):
        argument = f"__oapi_credential_{scheme.python_name}"
        names[scheme.wire_name] = argument
        if scheme.scheme_type == "apiKey":
            source = {"query": "query_params", "header": "headers", "cookie": "cookies"}[
                scheme.location or "header"
            ]
            value = f"__oapi_http.{source}.get({scheme.parameter_name!r}) or None"
        else:
            helper = "basic_auth" if scheme.scheme_type == "basic" else "bearer_auth"
            writer.require(".._runtime", f"{helper} as _runtime_{helper}")
            value = f"_runtime_{helper}(__oapi_http.headers.get('authorization'))"
        writer.line(f"{argument} = {value}", indent=2)

    nonempty_requirements = [
        requirement for requirement in operation.security if requirement.schemes
    ]
    empty_requirements = [
        requirement for requirement in operation.security if not requirement.schemes
    ]
    for requirement in [*nonempty_requirements, *empty_requirements]:
        if not requirement.schemes:
            writer.line("if not __oapi_security_satisfied:", indent=2)
            writer.line("__oapi_security_satisfied = True", indent=3)
            continue

        availability = " and ".join(
            f"{names[item.scheme_name]} is not None" for item in requirement.schemes
        )
        writer.line(f"if not __oapi_security_satisfied and {availability}:", indent=2)
        writer.line("try:", indent=3)
        writer.line("__oapi_alternative_context = None", indent=4)
        for item in requirement.schemes:
            scheme = security_schemes[item.scheme_name]
            argument = names[scheme.wire_name]
            writer.line(
                f"__oapi_alternative_context = await __oapi_security.handle_{scheme.python_name}(",
                indent=4,
            )
            writer.line("__oapi_alternative_context,", indent=5)
            writer.line(f"{operation.operation_id!r},", indent=5)
            writer.line(f"{scheme.class_name}(", indent=5)
            if scheme.scheme_type == "apiKey":
                writer.line(f"api_key={argument},", indent=6)
                writer.line(f"roles={item.scopes!r},", indent=6)
            elif scheme.scheme_type == "basic":
                writer.line(f"username={argument}.username,", indent=6)
                writer.line(f"password={argument}.password,", indent=6)
                writer.line(f"roles={item.scopes!r},", indent=6)
            elif scheme.scheme_type == "bearer":
                writer.line(f"token={argument}.credentials,", indent=6)
                writer.line(f"roles={item.scopes!r},", indent=6)
            else:
                writer.line(f"token={argument}.credentials,", indent=6)
                writer.line(f"scopes={item.scopes!r},", indent=6)
            writer.line(")", indent=5)
            writer.line(")", indent=4)
        writer.line("except SecurityRejected:", indent=3)
        writer.line("pass", indent=4)
        writer.line("else:", indent=3)
        writer.line("__oapi_security_context = __oapi_alternative_context", indent=4)
        writer.line("__oapi_security_satisfied = True", indent=4)

    writer.line("if not __oapi_security_satisfied:", indent=2)
    writer.line(
        "raise HTTPException(status_code=401, detail='security requirements are not satisfied')",
        indent=3,
    )
