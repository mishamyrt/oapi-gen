from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from msgspec import ValidationError, convert
from oapi_gen import GenerationError, check_package, generate_package

from .support import (
    ApiGenerator,
    SpecWriter,
    json_response,
    make_client,
    request_body,
)


@pytest.mark.parametrize(
    ("schema", "valid", "invalid"),
    [
        ({"type": "integer", "minimum": 1, "maximum": 5}, 2, [0, 6, "2"]),
        ({"type": "number", "minimum": 2, "exclusiveMinimum": 1, "multipleOf": 2}, 4, [1.5, 3]),
        (
            {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"},
            "ok",
            ["a", "longer", "12"],
        ),
        (
            {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {"$ref": "#/components/schemas/Positive"},
            },
            [1, 2],
            [[], [1], [1, 2, 3, 4], [0, 2]],
        ),
        (
            {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Positive"}},
            {"value": 1},
            [{"value": 0}],
        ),
        ({"type": "object", "additionalProperties": False}, {}, [{"extra": 1}]),
        (
            {"type": "object", "minProperties": 1, "maxProperties": 2},
            {"a": 1},
            [{}, {"a": 1, "b": 2, "c": 3}],
        ),
        ({"type": "object", "maxProperties": 0}, {}, [{"extra": 1}]),
        (
            {"type": ["object", "null"], "minProperties": 1, "maxProperties": 2},
            None,
            [{}, {"a": 1, "b": 2, "c": 3}],
        ),
        ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, "ok", [[], {}, None]),
        (
            {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 3}]},
            "yes",
            [0, "no"],
        ),
    ],
)
@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize("checked", [True, False])
def test_schema_constraints_apply_to_requests_and_responses(
    schema: dict[str, Any],
    valid: Any,
    invalid: list[Any],
    referenced: bool,
    checked: bool,
    generate_api: ApiGenerator,
) -> None:
    value_schema = {"$ref": "#/components/schemas/Value"} if referenced else schema
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(value_schema),
                    "responses": {"200": json_response(value_schema)},
                }
            },
        },
        {"Value": schema, "Positive": {"type": "integer", "minimum": 1}},
        validate_responses=checked,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    returned = valid
    received: list[Any] = []

    class Controller:
        async def test(self, request):
            received.append(request.body)
            return contracts.Test.Ok(body=returned)

    client = make_client(generated, Controller())
    response = client.post(
        "/value", content=json.dumps(valid), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == valid
    for value in invalid:
        assert client.post("/value", json=value).status_code == 422
    assert len(received) == 1
    for value in invalid:
        returned = value
        if checked:
            with pytest.raises(ValidationError):
                client.post(
                    "/value",
                    content=json.dumps(valid),
                    headers={"content-type": "application/json"},
                )
        else:
            assert (
                client.post(
                    "/value",
                    content=json.dumps(valid),
                    headers={"content-type": "application/json"},
                ).json()
                == value
            )
    returned = object()
    with pytest.raises(TypeError):
        client.post(
            "/value", content=json.dumps(valid), headers={"content-type": "application/json"}
        )


@pytest.mark.parametrize("scalar", [{"type": "string", "pattern": "^Thing$"}, {"const": "Thing"}])
def test_model_names_do_not_rewrite_patterns_or_literal_values(
    scalar: dict[str, Any], generate_api: ApiGenerator
) -> None:
    schema = {"anyOf": [{"$ref": "#/components/schemas/Thing"}, scalar]}
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(schema),
                    "responses": {"200": json_response(schema)},
                }
            },
        },
        {"Thing": {"type": "object", "properties": {"value": {"type": "string"}}}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=request.body)

    client = make_client(generated, Controller())
    response = client.post("/value", json="Thing")
    assert response.status_code == 200
    assert response.json() == "Thing"
    assert client.post("/value", json="_models.Thing").status_code == 422


@pytest.mark.parametrize("referenced", [False, True])
def test_unsupported_schema_constraints_fail_explicitly(
    referenced: bool, generate_api: ApiGenerator
) -> None:
    schema = {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}
    with pytest.raises(GenerationError, match="uniqueItems"):
        generate_api(
            {
                "/value": {
                    "post": {
                        "operationId": "test",
                        "requestBody": request_body(
                            {"$ref": "#/components/schemas/Value"} if referenced else schema
                        ),
                        "responses": {"204": {"description": "Empty"}},
                    }
                },
            },
            {"Value": schema} if referenced else None,
        )


def test_rejects_nested_external_references_before_model_generation(
    tmp_path: Path, write_specification: SpecWriter
) -> None:
    specification = write_specification(
        "external-reference.yaml",
        """
openapi: 3.1.0
info: {title: External reference, version: 1.0.0}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        '204': {description: Empty}
components:
  schemas:
    Unused:
      type: object
      properties:
        nested: {$ref: 'other.yaml#/components/schemas/Nested'}
""",
    )

    with pytest.raises(GenerationError, match=r"external .*ref"):
        generate_package(specification, tmp_path / "generated_external_reference")


def test_supports_recursive_internal_model_references(
    tmp_path: Path, write_specification: SpecWriter
) -> None:
    specification = write_specification(
        "recursive-reference.yaml",
        """
openapi: 3.1.0
info: {title: Recursive reference, version: 1.0.0}
paths:
  /node:
    get:
      operationId: getNode
      responses:
        '200':
          description: Node
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Node'}
  /recursive-array:
    get:
      operationId: getRecursiveArray
      responses:
        '200':
          description: Recursive array
          content:
            application/json:
              schema: {$ref: '#/components/schemas/RecursiveArray'}
components:
  schemas:
    Node:
      type: object
      required: [value]
      properties:
        value: {type: string}
        child: {$ref: '#/components/schemas/Node'}
    RecursiveArray:
      type: array
      items: {$ref: '#/components/schemas/RecursiveArray'}
""",
    )

    output = tmp_path / "generated_recursive_reference"
    generate_package(specification, output)
    check_package(specification, output)


@pytest.mark.parametrize("direction", ["readOnly", "writeOnly"])
def test_directional_fields_fail_before_writing(direction, generate_api, tmp_path):
    with pytest.raises(GenerationError, match=direction + ".*separate request and response"):
        generate_api(
            {},
            {
                "User": {
                    "type": "object",
                    "properties": {"secret": {"type": "string", direction: True}},
                }
            },
        )
    assert not any(path.is_dir() for path in tmp_path.iterdir())


@pytest.mark.parametrize("shape", ["scalar", "mapping", "array_mapping"])
def test_alias_chains_keep_strongest_bounds_in_bodies_models_and_collections(shape, generate_api):
    ref = {"$ref": "#/components/schemas/Alias"}
    value_schema = (
        {"$ref": "#/components/schemas/Values", "maxItems": 2}
        if shape == "array_mapping"
        else {**ref, "minimum": 12}
    )
    schemas = {
        "Base": {"type": "integer", "minimum": 10, "maximum": 20},
        "Alias": {"$ref": "#/components/schemas/Base", "minimum": 1, "maximum": 100},
        "Values": {"type": "array", "items": {**ref, "minimum": 12}},
        "Envelope": {
            "type": "object",
            "required": ["values"],
            "properties": {
                "values": {
                    "type": "object",
                    "additionalProperties": value_schema,
                },
            },
        },
    }
    body_schema = ref if shape == "scalar" else {"$ref": "#/components/schemas/Envelope"}
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(body_schema),
                    "responses": {"200": json_response(body_schema)},
                }
            }
        },
        schemas,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    valid = 10
    invalid = [3, 21]
    if shape == "mapping":
        valid = {"values": {"x": 12}}
        invalid = [{"values": {"x": n}} for n in (3, 10, 21)]
    elif shape == "array_mapping":
        valid = {"values": {"x": [12]}}
        invalid = [{"values": {"x": value}} for value in ([3], [10], [21], [12, 12, 12])]
    returned = valid

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=returned)

    client = make_client(generated, Controller())
    assert client.post("/value", json=valid).json() == valid
    for value in invalid:
        assert client.post("/value", json=value).status_code == 422
        returned = value
        with pytest.raises(ValidationError):
            client.post("/value", json=valid)
    # Normalization for code generation must not rewrite the published contract.
    assert client.get("/openapi.json").json()["components"]["schemas"] == schemas


@pytest.mark.parametrize("referenced", [False, True])
def test_overlapping_oneof_fails_before_writing(referenced, generate_api, tmp_path):
    schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    with pytest.raises(GenerationError, match=r"oneOf.*disjoint"):
        generate_api(
            {
                "/value": {
                    "post": {
                        "operationId": "test",
                        "requestBody": request_body(
                            {"$ref": "#/components/schemas/Value"} if referenced else schema
                        ),
                        "responses": {"204": {"description": "OK"}},
                    }
                }
            },
            {"Value": schema} if referenced else None,
        )
    assert not any(path.is_dir() for path in tmp_path.iterdir())


@pytest.mark.parametrize("nullable", [False, True])
def test_optional_json_body_distinguishes_missing_from_null(nullable, generate_api):
    schema = {"type": ["integer", "null"]} if nullable else {"type": "integer"}
    body = request_body(schema)
    body["required"] = False
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": body,
                    "responses": {"204": {"description": "OK"}},
                }
            }
        }
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    received = []

    class Controller:
        async def test(self, request):
            received.append(request.body)
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    assert client.post("/value").status_code == 204
    assert client.post("/value", json=2).status_code == 204
    assert client.post(
        "/value", content="null", headers={"content-type": "application/json"}
    ).status_code == (204 if nullable else 422)
    assert received == ([None, 2, None] if nullable else [None, 2])


def test_tagged_oneof_remains_supported(generate_api):
    variants = {
        name: {
            "type": "object",
            "required": ["kind", "value"],
            "properties": {
                "kind": {"type": "string", "const": name},
                "value": {"type": value_type},
            },
        }
        for name, value_type in [("Text", "string"), ("Count", "integer")]
    }
    schema = {
        "oneOf": [{"$ref": "#/components/schemas/" + name} for name in variants],
        "discriminator": {"propertyName": "kind"},
    }
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body({"$ref": "#/components/schemas/Value"}),
                    "responses": {"200": json_response({"$ref": "#/components/schemas/Value"})},
                }
            }
        },
        {**variants, "Value": schema},
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=request.body)

    client = make_client(generated, Controller())
    for value in [{"kind": "Text", "value": "ok"}, {"kind": "Count", "value": 2}]:
        assert client.post("/value", json=value).json() == value
    assert client.post("/value", json={"kind": "Count", "value": "wrong"}).status_code == 422


@pytest.mark.parametrize(
    "shape", ["model", "array", "mapping", "recursive", "union", "nested_union", "allOf"]
)
def test_property_counts_on_models_and_nested_wire_objects(shape, generate_api):
    ref = {"$ref": "#/components/schemas/Alias", "maxProperties": 2}
    schemas = {
        "Value": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 3,
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
        "Alias": {"$ref": "#/components/schemas/Value", "minProperties": 0, "maxProperties": 5},
    }
    schema = ref
    valid = {"a": 1, "b": 2}
    invalid = [{}, {"a": 1, "b": 2, "extra": 3}]
    if shape == "array":
        schema = {"type": "array", "items": ref}
        valid, invalid = [valid], [[value] for value in invalid]
    elif shape in {"mapping", "recursive"}:
        schema = {"$ref": "#/components/schemas/Envelope"}
        schemas["Envelope"] = {
            "type": "object",
            "required": ["values"],
            "properties": {"values": {"type": "object", "additionalProperties": ref}},
        }
        valid, invalid = {"values": {"x": valid}}, [{"values": {"x": value}} for value in invalid]
        if shape == "recursive":
            schemas["Envelope"]["properties"]["child"] = schema
            valid = {"values": {}, "child": valid}
            invalid = [{"values": {}, "child": value} for value in invalid]
    elif shape == "union":
        schema = {"anyOf": [ref, {"type": "string"}, {"type": "null"}]}
    elif shape == "nested_union":
        schema = {"anyOf": [ref, {"anyOf": [{"type": "string"}, {"type": "null"}]}]}
    elif shape == "allOf":
        schema = {"$ref": "#/components/schemas/Composed"}
        schemas["Composed"] = {"allOf": [ref], "maxProperties": 2}
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(schema),
                    "responses": {"200": json_response(schema)},
                }
            }
        },
        schemas,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    returned = valid

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=returned)

    client = make_client(generated, Controller())
    assert client.post("/value", json=valid).status_code == 200
    for value in invalid:
        response = client.post("/value", json=value)
        assert response.status_code == 422
        assert "Properties" in response.json()["detail"][0]["msg"]
    if shape in {"union", "nested_union"}:
        assert client.post("/value", json="ok").status_code == 200
        assert (
            client.post(
                "/value", content="null", headers={"content-type": "application/json"}
            ).status_code
            == 200
        )
    assert client.get("/openapi.json").json()["components"]["schemas"] == schemas


@pytest.mark.parametrize("keyword", ["minProperties", "maxProperties"])
@pytest.mark.parametrize("bound", [-1, True, 1.5, "2", None])
def test_invalid_property_bounds_fail_before_writing(keyword, bound, generate_api, tmp_path):
    with pytest.raises(GenerationError, match=keyword + " must be a non-negative integer"):
        generate_api({}, {"Value": {"type": "object", keyword: bound}})
    assert not any(path.is_dir() for path in tmp_path.iterdir())


@pytest.mark.parametrize("union", ["oneOf", "anyOf"])
def test_property_counts_follow_discriminator_branch(union, generate_api):
    schemas = {
        "Small": {
            "type": "object",
            "required": ["kind"],
            "maxProperties": 1,
            "properties": {"kind": {"type": "string", "const": "small"}},
        },
        "Large": {
            "type": "object",
            "required": ["kind"],
            "minProperties": 2,
            "maxProperties": 2,
            "properties": {
                "kind": {"type": "string", "const": "large"},
                "value": {"type": "string"},
            },
        },
        "Value": {
            union: [{"$ref": "#/components/schemas/Small"}, {"$ref": "#/components/schemas/Large"}],
            "discriminator": {"propertyName": "kind"},
        },
    }
    schema = {"$ref": "#/components/schemas/Value"}
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(schema),
                    "responses": {"200": json_response(schema)},
                }
            }
        },
        schemas,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=request.body)

    client = make_client(generated, Controller())
    for value in [{"kind": "small"}, {"kind": "large", "value": "ok"}]:
        assert client.post("/value", json=value).json() == value
    for value in [
        {"kind": "small", "extra": 1},
        {"kind": "large"},
        {"kind": "large", "value": "ok", "extra": 1},
    ]:
        assert client.post("/value", json=value).status_code == 422


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("bound", [{"minProperties": 3}, {"maxProperties": 1}])
def test_response_property_counts_include_serialized_model_defaults(checked, bound, generate_api):
    schema = {"anyOf": [{"$ref": "#/components/schemas/Value"}, {"type": "string"}]}
    generated = generate_api(
        {"/value": {"get": {"operationId": "test", "responses": {"200": json_response(schema)}}}},
        {
            "Value": {
                "type": "object",
                **bound,
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer", "default": 2}},
            }
        },
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=models.Value(a=1))

    client = make_client(generated, Controller())
    if checked:
        with pytest.raises(ValidationError, match=next(iter(bound))):
            client.get("/value")
    else:
        assert client.get("/value").json() == {"a": 1, "b": 2}


def test_property_count_aliases_preserve_dictionary_value_types(generate_api):
    schema = {"$ref": "#/components/schemas/Envelope"}
    schemas = {
        "Value": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 2,
            "additionalProperties": {"type": "integer"},
        },
        "Alias": {"$ref": "#/components/schemas/Value", "minProperties": 0, "maxProperties": 5},
        "Envelope": {
            "type": "object",
            "required": ["values"],
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/Alias", "minProperties": 2},
                }
            },
        },
    }
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body(schema),
                    "responses": {"200": json_response(schema)},
                }
            }
        },
        schemas,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=request.body)

    client = make_client(generated, Controller())
    valid = {"values": [{"a": 1, "b": 2}]}
    assert client.post("/value", json=valid).json() == valid
    for value in [{"a": 1}, {"a": 1, "b": 2, "c": 3}, {"a": 1, "b": "wrong"}]:
        invalid = {"values": [value]}
        assert client.post("/value", json=invalid).status_code == 422
        with pytest.raises(ValidationError):
            convert(invalid, type=models.Envelope)
    for value in [{}, {"a": 1, "b": 2, "c": 3}]:
        with pytest.raises(ValidationError):
            convert(value, type=models.Alias)
    assert client.get("/openapi.json").json()["components"]["schemas"] == schemas
