"""Parse security schemes, OAuth flows, and ordered security requirements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..errors import GenerationError
from ..ir import (
    ParameterLocation,
    SecurityRequirement,
    SecurityRequirementItem,
    SecurityScheme,
    SecuritySchemeType,
)
from ..naming import pascal_case, snake_case
from .references import Resolver
from .values import array_value, object_value, optional_string, required_string

_API_KEY_LOCATIONS = {"query", "header", "cookie"}
_OAUTH_FLOW_REQUIREMENTS = {
    "implicit": ("authorizationUrl",),
    "password": ("tokenUrl",),
    "clientCredentials": ("tokenUrl",),
    "authorizationCode": ("authorizationUrl", "tokenUrl"),
}


def parse_security_schemes(
    document: Mapping[str, Any], resolver: Resolver
) -> tuple[SecurityScheme, ...]:
    components = object_value(document.get("components", {}), "components")
    raw_schemes = object_value(components.get("securitySchemes", {}), "components.securitySchemes")
    if not all(isinstance(name, str) and name for name in raw_schemes):
        raise GenerationError("components.securitySchemes keys must be non-empty strings")
    schemes: list[SecurityScheme] = []
    for wire_name in sorted(raw_schemes):
        context = f"components.securitySchemes.{wire_name}"
        raw = resolver.resolve_object(raw_schemes[wire_name], context)
        raw_type = required_string(raw, "type", context)
        description = optional_string(raw.get("description"), f"{context}.description")
        location: ParameterLocation | None = None
        parameter_name: str | None = None
        bearer_format: str | None = None
        flows: dict[str, object] | None = None

        if raw_type == "apiKey":
            parameter_name = required_string(raw, "name", context)
            raw_location = required_string(raw, "in", context)
            if raw_location not in _API_KEY_LOCATIONS:
                raise GenerationError(f"{context}.in must be one of {sorted(_API_KEY_LOCATIONS)!r}")
            location = cast(ParameterLocation, raw_location)
            scheme_type: SecuritySchemeType = "apiKey"
        elif raw_type == "http":
            http_scheme = required_string(raw, "scheme", context).lower()
            if http_scheme not in {"basic", "bearer"}:
                raise GenerationError(
                    f"{context}: unsupported HTTP security scheme {http_scheme!r}"
                )
            scheme_type = cast(SecuritySchemeType, http_scheme)
            bearer_format = optional_string(raw.get("bearerFormat"), f"{context}.bearerFormat")
        elif raw_type == "oauth2":
            scheme_type = "oauth2"
            flows = _parse_oauth_flows(raw.get("flows"), context)
        elif raw_type in {"mutualTLS", "openIdConnect"}:
            raise GenerationError(f"{context}: {raw_type} security is not supported yet")
        else:
            raise GenerationError(f"{context}: unknown security scheme type {raw_type!r}")

        try:
            python_name = snake_case(wire_name)
            class_name = f"{pascal_case(wire_name)}Security"
        except ValueError as error:
            raise GenerationError(f"{context}: invalid security scheme name") from error
        schemes.append(
            SecurityScheme(
                wire_name=wire_name,
                python_name=python_name,
                class_name=class_name,
                scheme_type=scheme_type,
                description=description,
                location=location,
                parameter_name=parameter_name,
                bearer_format=bearer_format,
                flows=flows,
            )
        )

    python_names = [scheme.python_name for scheme in schemes]
    class_names = [scheme.class_name for scheme in schemes]
    if len(python_names) != len(set(python_names)) or len(class_names) != len(set(class_names)):
        raise GenerationError("security scheme names collide after Python name normalization")
    return tuple(schemes)


def _parse_oauth_flows(value: object, context: str) -> dict[str, object]:
    raw_flows = object_value(value, f"{context}.flows")
    if not raw_flows:
        raise GenerationError(f"{context}.flows must contain at least one OAuth2 flow")
    if not all(isinstance(name, str) for name in raw_flows):
        raise GenerationError(f"{context}.flows keys must be strings")
    unknown = sorted(set(raw_flows) - set(_OAUTH_FLOW_REQUIREMENTS))
    if unknown:
        raise GenerationError(f"{context}.flows contains unsupported flows: {unknown!r}")

    flows: dict[str, object] = {}
    for flow_name in _OAUTH_FLOW_REQUIREMENTS:
        if flow_name not in raw_flows:
            continue
        flow_context = f"{context}.flows.{flow_name}"
        raw_flow = object_value(raw_flows[flow_name], flow_context)
        flow: dict[str, object] = {}
        for field_name in _OAUTH_FLOW_REQUIREMENTS[flow_name]:
            flow[field_name] = required_string(raw_flow, field_name, flow_context)
        refresh_url = optional_string(raw_flow.get("refreshUrl"), f"{flow_context}.refreshUrl")
        if refresh_url is not None:
            flow["refreshUrl"] = refresh_url
        raw_scopes = object_value(raw_flow.get("scopes"), f"{flow_context}.scopes")
        if not all(
            isinstance(name, str) and isinstance(item, str) for name, item in raw_scopes.items()
        ):
            raise GenerationError(f"{flow_context}.scopes must map strings to strings")
        flow["scopes"] = dict(raw_scopes)
        flows[flow_name] = flow
    return flows


def parse_security_requirements(
    value: object,
    schemes: Mapping[str, SecurityScheme],
    context: str,
) -> tuple[SecurityRequirement, ...]:
    if value is None:
        return ()
    raw_requirements = array_value(value, context)
    requirements: list[SecurityRequirement] = []
    for index, value in enumerate(raw_requirements):
        raw = object_value(value, f"{context}[{index}]")
        if not all(isinstance(name, str) and name for name in raw):
            raise GenerationError(f"{context}[{index}] keys must be non-empty strings")
        items: list[SecurityRequirementItem] = []
        for scheme_name in raw:
            if scheme_name not in schemes:
                raise GenerationError(
                    f"{context}[{index}]: unknown security scheme {scheme_name!r}"
                )
            raw_scopes = array_value(raw[scheme_name], f"{context}[{index}].{scheme_name}")
            if not all(isinstance(scope, str) for scope in raw_scopes):
                raise GenerationError(f"{context}[{index}].{scheme_name} must contain strings")
            items.append(
                SecurityRequirementItem(
                    scheme_name=scheme_name,
                    scopes=tuple(cast(list[str], raw_scopes)),
                )
            )
        requirements.append(SecurityRequirement(schemes=tuple(items)))
    return tuple(requirements)
