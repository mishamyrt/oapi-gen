from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any, Literal

ParameterLocation = Literal["path", "query", "header", "cookie"]
SecuritySchemeType = Literal["apiKey", "basic", "bearer", "oauth2"]
ImportRef = tuple[str, str]
Constraints = tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class TypeRef:
    annotation: str
    model_names: frozenset[str] = frozenset()
    imports: frozenset[ImportRef] = frozenset()
    nullable: bool = False
    constraints: Constraints = ()

    @property
    def annotated(self) -> str:
        if not self.constraints:
            return self.annotation
        values = dict(self.constraints)
        # msgspec accepts one bound per side; retain the strongest constraint.
        for inclusive, exclusive, select in (("ge", "gt", max), ("le", "lt", min)):
            included = values.get(inclusive)
            excluded = values.get(exclusive)
            if isinstance(included, (int, float)) and isinstance(excluded, (int, float)):
                del values[inclusive if select(included, excluded) == excluded else exclusive]
        options = ", ".join(f"{name}={value!r}" for name, value in values.items())
        return f"Annotated[{self.annotation}, Meta({options})]"

    @property
    def required_imports(self) -> frozenset[ImportRef]:
        if not self.constraints:
            return self.imports
        return self.imports | {("typing", "Annotated"), ("msgspec", "Meta")}

    def constrained(self, constraints: Constraints) -> TypeRef:
        values = dict(self.constraints)
        for name, value in constraints:
            previous = values.get(name)
            if isinstance(previous, (int, float)) and isinstance(value, (int, float)):
                if name in {"ge", "gt", "min_length"}:
                    value = max(previous, value)
                elif name in {"le", "lt", "max_length"}:
                    value = min(previous, value)
            if name in {"pattern", "multiple_of"} and name in values and value != previous:
                raise ValueError(f"combining different {name} constraints is not supported")
            values[name] = value
        return replace(self, constraints=tuple(values.items()))

    def optional(self) -> TypeRef:
        if self.nullable:
            return self
        return TypeRef(
            annotation=f"{self.annotated} | None",
            model_names=self.model_names,
            imports=self.required_imports,
            nullable=True,
        )


@dataclass(frozen=True, slots=True)
class Parameter:
    wire_name: str
    python_name: str
    location: ParameterLocation
    type_ref: TypeRef
    required: bool
    description: str | None
    default: object | None
    has_default: bool
    serialization: Literal["native", "comma-separated"]
    wire_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class RequestBody:
    python_name: str
    type_ref: TypeRef
    required: bool
    description: str | None
    media_type: str
    multipart_fields: tuple[MultipartField, ...] = ()
    property_counts: dict[str, Any] | None = None

    @property
    def is_multipart(self) -> bool:
        return self.media_type == "multipart/form-data"


@dataclass(frozen=True, slots=True)
class MultipartField:
    wire_name: str
    python_name: str
    type_ref: TypeRef
    required: bool
    description: str | None
    is_file: bool
    is_array: bool


@dataclass(frozen=True, slots=True)
class ResponseHeader:
    wire_name: str
    python_name: str
    type_ref: TypeRef
    required: bool
    description: str | None
    is_cookie_array: bool = False


@dataclass(frozen=True, slots=True)
class EventField:
    python_name: str
    type_ref: TypeRef
    wire_type_ref: TypeRef
    required: bool
    description: str | None
    json_encoded: bool = False
    property_counts: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Response:
    status_code: int
    class_name: str
    type_ref: TypeRef | None
    description: str | None
    media_type: str | None
    headers: tuple[ResponseHeader, ...]
    property_counts: dict[str, Any] | None = None
    summary: str | None = None
    streaming: bool = False
    event_fields: tuple[EventField, ...] = ()

    @property
    def event_class_name(self) -> str:
        return "Event" if self.status_code == 200 else f"{self.class_name}Event"


@dataclass(frozen=True, slots=True)
class SecurityScheme:
    wire_name: str
    python_name: str
    class_name: str
    scheme_type: SecuritySchemeType
    description: str | None
    location: ParameterLocation | None
    parameter_name: str | None
    flows: dict[str, object] | None
    oauth2_metadata_url: str | None = None
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class SecurityRequirementItem:
    scheme_name: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecurityRequirement:
    schemes: tuple[SecurityRequirementItem, ...]


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    operation_id: str
    python_name: str
    class_name: str
    group_name: str
    group_class_name: str
    group_field_name: str
    summary: str | None
    description: str | None
    deprecated: bool
    parameters: tuple[Parameter, ...]
    request_body: RequestBody | None
    responses: tuple[Response, ...]
    security: tuple[SecurityRequirement, ...]

    def type_refs(self) -> Iterator[TypeRef]:
        for parameter in self.parameters:
            yield parameter.type_ref
        if self.request_body is not None:
            yield self.request_body.type_ref
            for field in self.request_body.multipart_fields:
                yield field.type_ref
        for response in self.responses:
            if response.type_ref is not None:
                yield response.type_ref
            for field in response.event_fields:
                yield field.type_ref
                yield field.wire_type_ref
            for header in response.headers:
                yield header.type_ref


@dataclass(frozen=True, slots=True)
class HandlerGroup:
    class_name: str
    field_name: str
    operations: tuple[Operation, ...]


@dataclass(frozen=True, slots=True)
class ApiSpec:
    title: str
    api_version: str
    source_hash: str
    operations: tuple[Operation, ...]
    groups: tuple[HandlerGroup, ...]
    security_schemes: tuple[SecurityScheme, ...]
