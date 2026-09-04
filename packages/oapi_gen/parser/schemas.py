"""Translate supported schemas to IR types and validate component schema references."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from ..errors import GenerationError
from ..ir import (
    ImportRef,
    TypeRef,
)
from ..naming import pascal_case
from .references import Resolver
from .values import array_value, object_value

_CONSTRAINT_KEYS = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minLength": "min_length",
    "maxLength": "max_length",
    "minItems": "min_length",
    "maxItems": "max_length",
    "pattern": "pattern",
    "multipleOf": "multiple_of",
}
_PROPERTY_COUNT_KEYS = ("minProperties", "maxProperties")


def schema_constraints(schema: Mapping[str, Any], context: str) -> tuple[tuple[str, object], ...]:
    unsupported = {
        "contains",
        "minContains",
        "maxContains",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "prefixItems",
    } & schema.keys()
    for name in _PROPERTY_COUNT_KEYS:
        if name in schema and (type(schema[name]) is not int or schema[name] < 0):
            raise GenerationError(f"{context}: {name} must be a non-negative integer")
    for name in ("readOnly", "writeOnly"):
        if schema.get(name) is True:
            raise GenerationError(
                f"{context}: {name} is not supported; use separate request and response schemas"
            )
    if schema.get("uniqueItems") is True:
        unsupported.add("uniqueItems")
    if unsupported:
        raise GenerationError(
            f"{context}: unsupported schema constraints: {', '.join(sorted(unsupported))}"
        )
    result: list[tuple[str, object]] = []
    for source, target in _CONSTRAINT_KEYS.items():
        if source in schema and not source.startswith("exclusive"):
            result.append((target, schema[source]))

    exclusive_minimum = schema.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, (int, float)) and not isinstance(exclusive_minimum, bool):
        result.append(("gt", exclusive_minimum))
    elif exclusive_minimum is True and "minimum" in schema:
        result = [item for item in result if item[0] != "ge"]
        result.append(("gt", schema["minimum"]))

    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool):
        result.append(("lt", exclusive_maximum))
    elif exclusive_maximum is True and "maximum" in schema:
        result = [item for item in result if item[0] != "le"]
        result.append(("lt", schema["maximum"]))
    return tuple(result)


def resolve_schema(schema: object, resolver: Resolver, context: str) -> dict[str, Any]:
    """Resolve aliases without replacing a target's bounds with weaker sibling bounds."""
    result = resolver.resolve_object(schema, context)
    current = object_value(schema, context)
    combined = TypeRef("Any")
    property_bounds: dict[str, int] = {}
    while True:
        try:
            combined = combined.constrained(schema_constraints(current, context))
        except ValueError as error:
            raise GenerationError(f"{context}: {error}") from error
        for name, select in (("minProperties", max), ("maxProperties", min)):
            if name in current:
                property_bounds[name] = select(
                    property_bounds.get(name, current[name]), current[name]
                )
        if "$ref" not in current:
            break
        current = object_value(resolver.resolve_ref(current["$ref"], context), context)
    result = {key: value for key, value in result.items() if key not in _CONSTRAINT_KEYS}
    raw_type = result.get("type")
    is_array = raw_type == "array" or (isinstance(raw_type, list) and "array" in raw_type)
    values = dict(combined.constraints)
    for source, target in _CONSTRAINT_KEYS.items():
        if source.endswith("Items") != is_array and source.endswith(("Items", "Length")):
            continue
        if target in values:
            result[source] = values[target]
    result.update(property_bounds)
    return result


def validate_one_of(schema: Mapping[str, Any], resolver: Resolver, context: str) -> None:
    if "oneOf" not in schema:
        return
    members = array_value(schema["oneOf"], f"{context}.oneOf")
    resolved = [resolve_schema(member, resolver, f"{context}.oneOf") for member in members]
    discriminator = schema.get("discriminator")
    if (
        isinstance(discriminator, dict)
        and isinstance(tag := discriminator.get("propertyName"), str)
        and resolved
        and all(raw.get("type") == "object" and raw.get("nullable") is not True for raw in resolved)
    ):
        tags: set[str] = set()
        for raw in resolved:
            properties = object_value(raw.get("properties", {}), context)
            field = resolve_schema(properties.get(tag, {}), resolver, context)
            values = field.get("enum", [field["const"]] if "const" in field else [])
            if (
                tag not in raw.get("required", [])
                or not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) for value in values)
                or tags.intersection(values)
            ):
                raise GenerationError(
                    f"{context}: oneOf discriminator must be required "
                    "with disjoint string const/enum values"
                )
            tags.update(values)
        return
    seen: set[str] = set()
    for raw in resolved:
        raw_type = raw.get("type")
        raw_types = raw_type if isinstance(raw_type, list) else [raw_type]
        if not raw_types or not all(isinstance(item, str) for item in raw_types):
            raise GenerationError(f"{context}: oneOf requires explicit JSON types")
        types = set(cast(list[str], raw_types))
        if raw.get("nullable") is True:
            types.add("null")
        if "number" in types:
            types.add("integer")
        # ponytail: prove disjoint types/tags, reject unions requiring a general overlap solver.
        if types & seen:
            raise GenerationError(
                f"{context}: oneOf requires disjoint explicit JSON types; "
                "use anyOf if overlap is intended (object unions require a discriminator)"
            )
        seen.update(types)
    if not members:
        raise GenerationError(f"{context}: oneOf must not be empty")


def prepare_component_schemas(document: Mapping[str, Any], resolver: Resolver) -> dict[str, Any]:
    components = object_value(document.get("components", {}), "components")
    schemas = deepcopy(object_value(components.get("schemas", {}), "components.schemas"))
    seen: set[int] = set()

    generated_names = {pascal_case(name) for name in schemas}
    constrained_aliases: dict[tuple[str, tuple[tuple[str, object], ...]], str] = {}

    def visit(schema: object, context: str, *, root: bool = False) -> None:
        raw = object_value(schema, context)
        identity = id(raw)
        if identity in seen:
            return
        seen.add(identity)
        constraints = schema_constraints(raw, context)
        validate_one_of(raw, resolver, context)
        if (
            raw.get("additionalProperties") is False
            and not raw.get("properties")
            and "$ref" not in raw
        ):
            raw["maxProperties"] = 0
        ref = raw.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
                if isinstance(ref, str) and not ref.startswith("#/"):
                    raise GenerationError(
                        f"{context}: external $ref values are not supported: {ref!r}"
                    )
                raise GenerationError(f"{context}: schema $ref must point to components/schemas")
            resolved = resolve_schema(raw, resolver, context)
            if root and SchemaParser._is_collapsible_root_schema(resolved):
                raw.clear()
                raw.update(deepcopy(resolved))
            elif constraints or any(key in raw for key in _PROPERTY_COUNT_KEYS):
                for key in (*_CONSTRAINT_KEYS, *_PROPERTY_COUNT_KEYS):
                    raw.pop(key, None)
                    if key in resolved:
                        raw[key] = resolved[key]
                if not root:
                    # datamodel-code-generator loses sibling bounds in collection values.
                    # A named constrained alias preserves them throughout nested models.
                    key = (
                        ref,
                        schema_constraints(resolved, context)
                        + tuple(
                            (key, resolved[key]) for key in _PROPERTY_COUNT_KEYS if key in resolved
                        ),
                    )
                    name = constrained_aliases.get(key)
                    if name is None:
                        index = len(generated_names)
                        while (name := f"OapiConstrained{index}") in generated_names:
                            index += 1
                        generated_names.add(name)
                        constrained_aliases[key] = name
                        schemas[name] = deepcopy(resolved)
                        visit(schemas[name], f"components.schemas.{name}", root=True)
                    for key in (*_CONSTRAINT_KEYS, *_PROPERTY_COUNT_KEYS):
                        raw.pop(key, None)
                    raw["$ref"] = f"#/components/schemas/{name}"

        for keyword in (
            "additionalProperties",
            "contains",
            "contentSchema",
            "else",
            "if",
            "items",
            "not",
            "propertyNames",
            "then",
            "unevaluatedItems",
            "unevaluatedProperties",
        ):
            child = raw.get(keyword)
            if isinstance(child, dict):
                visit(child, f"{context}.{keyword}")
        for keyword in (
            "$defs",
            "definitions",
            "dependentSchemas",
            "patternProperties",
            "properties",
        ):
            children = raw.get(keyword)
            if isinstance(children, dict):
                for name, child in children.items():
                    visit(child, f"{context}.{keyword}.{name}")
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = raw.get(keyword)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    visit(child, f"{context}.{keyword}[{index}]")

    for name, schema in list(schemas.items()):
        visit(schema, f"components.schemas.{name}", root=True)
    return schemas


class SchemaParser:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    def property_counts(self, schema: Mapping[str, Any], context: str) -> dict[str, Any] | None:
        """Keep only the schema structure needed to count properties on wire values."""
        references: dict[str, Any] = {}
        bounded = False

        def visit(raw: Mapping[str, Any]) -> dict[str, Any]:
            nonlocal bounded
            schema_constraints(raw, context)
            bounded |= "minProperties" in raw or "maxProperties" in raw
            result = {
                key: raw[key]
                for key in (
                    "$ref",
                    "type",
                    "nullable",
                    "const",
                    "enum",
                    "discriminator",
                    "minProperties",
                    "maxProperties",
                )
                if key in raw
            }
            if (ref := raw.get("$ref")) is not None and ref not in references:
                references[ref] = {}
                references[ref] = visit(
                    object_value(self._resolver.resolve_ref(ref, context), context)
                )
            if "properties" in raw:
                result["properties"] = {
                    name: visit(child) for name, child in raw["properties"].items()
                }
            for key in ("items", "additionalProperties"):
                if isinstance(child := raw.get(key), dict):
                    result[key] = visit(child)
            for key in ("allOf", "anyOf", "oneOf"):
                if key in raw:
                    result[key] = [visit(child) for child in raw[key]]
            return result

        root = visit(schema)
        return {"schema": root, "references": references} if bounded else None

    def parse(
        self,
        schema: Mapping[str, Any],
        context: str,
        resolving_refs: frozenset[str] = frozenset(),
    ) -> TypeRef:
        constraints = schema_constraints(schema, context)
        validate_one_of(schema, self._resolver, context)
        result = self._schema_base_type(schema, context, resolving_refs)
        try:
            return result.constrained(constraints)
        except ValueError as error:
            raise GenerationError(f"{context}: {error}") from error

    def _schema_base_type(
        self,
        schema: Mapping[str, Any],
        context: str,
        resolving_refs: frozenset[str],
    ) -> TypeRef:
        ref = schema.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
                if isinstance(ref, str) and not ref.startswith("#/"):
                    raise GenerationError(
                        f"{context}: external $ref values are not supported: {ref!r}"
                    )
                raise GenerationError(f"{context}: schema $ref must point to components/schemas")
            raw_name = (
                ref.removeprefix("#/components/schemas/").replace("~1", "/").replace("~0", "~")
            )
            if "/" in raw_name:
                raise GenerationError(
                    f"{context}: nested schema references are not supported: {ref!r}"
                )
            name = pascal_case(raw_name)
            if ref in resolving_refs:
                result = TypeRef(annotation=f"_models_{name}", model_names=frozenset({name}))
                return result.optional() if schema.get("nullable") is True else result
            target = self._resolver.resolve_ref(ref, context)
            if not isinstance(target, dict):
                raise GenerationError(f"{context}: schema $ref must point to an object")
            target_schema = resolve_schema(target, self._resolver, context)
            if self._is_collapsible_root_schema(target_schema):
                result = self.parse(
                    target_schema,
                    f"{context} ({ref})",
                    resolving_refs | {ref},
                )
                return result.optional() if schema.get("nullable") is True else result
            result = TypeRef(annotation=f"_models_{name}", model_names=frozenset({name}))
            return result.optional() if schema.get("nullable") is True else result

        if "const" in schema:
            value = schema["const"]
            return TypeRef(
                annotation=f"Literal[{value!r}]",
                imports=frozenset({("typing", "Literal")}),
                nullable=value is None,
            )
        if "enum" in schema:
            enum = array_value(schema["enum"], f"{context}.enum")
            if not enum:
                raise GenerationError(f"{context}: enum must not be empty")
            nullable = None in enum
            values = ", ".join(repr(item) for item in enum)
            return TypeRef(
                annotation=f"Literal[{values}]",
                imports=frozenset({("typing", "Literal")}),
                nullable=nullable,
            )

        for union_keyword in ("oneOf", "anyOf"):
            if union_keyword in schema:
                members = array_value(schema[union_keyword], f"{context}.{union_keyword}")
                types = [
                    self.parse(
                        object_value(member, f"{context}.{union_keyword}"),
                        context,
                        resolving_refs,
                    )
                    for member in members
                ]
                return self._union_type(types, schema.get("nullable") is True)

        if "allOf" in schema:
            members = array_value(schema["allOf"], f"{context}.allOf")
            if len(members) != 1:
                raise GenerationError(
                    f"{context}: inline allOf composition is not supported; "
                    "move it to components/schemas"
                )
            result = self.parse(
                object_value(members[0], f"{context}.allOf[0]"),
                context,
                resolving_refs,
            )
            return result.optional() if schema.get("nullable") is True else result

        raw_type = schema.get("type")
        nullable = schema.get("nullable") is True
        if isinstance(raw_type, list):
            raw_types = [item for item in raw_type if item != "null"]
            nullable = nullable or len(raw_types) != len(raw_type)
            types = [
                self.parse({**schema, "type": item}, context, resolving_refs) for item in raw_types
            ]
            return self._union_type(types, nullable)

        result: TypeRef
        if raw_type == "string":
            format_name = schema.get("format")
            formats: dict[str, TypeRef] = {
                "date": TypeRef("date", imports=frozenset({("datetime", "date")})),
                "date-time": TypeRef("datetime", imports=frozenset({("datetime", "datetime")})),
                "uuid": TypeRef("UUID", imports=frozenset({("uuid", "UUID")})),
                "binary": TypeRef("bytes"),
                "byte": TypeRef("bytes"),
            }
            result = formats.get(cast(str, format_name), TypeRef("str"))
        elif raw_type == "integer":
            result = TypeRef("int")
        elif raw_type == "number":
            result = TypeRef("float")
        elif raw_type == "boolean":
            result = TypeRef("bool")
        elif raw_type == "array":
            items = object_value(schema.get("items"), f"{context}.items")
            item_type = self.parse(items, f"{context}.items", resolving_refs)
            result = TypeRef(
                annotation=f"list[{item_type.annotated}]",
                model_names=item_type.model_names,
                imports=item_type.required_imports,
            )
        elif raw_type == "object" or "additionalProperties" in schema:
            if schema.get("properties"):
                raise GenerationError(
                    f"{context}: inline object schemas are not supported; "
                    "move the schema to components/schemas"
                )
            additional = schema.get("additionalProperties", True)
            value_type = (
                self.parse(
                    object_value(additional, f"{context}.additionalProperties"),
                    context,
                    resolving_refs,
                )
                if isinstance(additional, dict)
                else TypeRef("Any", imports=frozenset({("typing", "Any")}))
            )
            result = TypeRef(
                annotation=f"dict[str, {value_type.annotated}]",
                model_names=value_type.model_names,
                imports=value_type.required_imports,
                constraints=(("max_length", 0),) if additional is False else (),
            )
        elif raw_type == "null":
            result = TypeRef("None", nullable=True)
        elif raw_type is None:
            result = TypeRef("Any", imports=frozenset({("typing", "Any")}))
        else:
            raise GenerationError(f"{context}: unsupported schema type {raw_type!r}")
        return result.optional() if nullable else result

    @staticmethod
    def _is_collapsible_root_schema(schema: Mapping[str, Any]) -> bool:
        raw_type = schema.get("type")
        return (
            isinstance(raw_type, str)
            and raw_type
            in {
                "array",
                "boolean",
                "integer",
                "null",
                "number",
                "string",
            }
            and not any(key in schema for key in ("const", "enum"))
        )

    @staticmethod
    def _union_type(types: list[TypeRef], nullable: bool) -> TypeRef:
        annotations: list[str] = []
        model_names: set[str] = set()
        imports: set[ImportRef] = set()
        has_none = nullable
        for type_ref in types:
            if type_ref.annotated not in annotations and type_ref.annotation != "None":
                annotations.append(type_ref.annotated)
            model_names.update(type_ref.model_names)
            imports.update(type_ref.required_imports)
            has_none = has_none or type_ref.nullable or type_ref.annotation == "None"
        if not annotations:
            return TypeRef("None", nullable=True)
        annotation = " | ".join(annotations)
        if has_none and "None" not in annotations:
            annotation = f"{annotation} | None"
        return TypeRef(annotation, frozenset(model_names), frozenset(imports), has_none)
