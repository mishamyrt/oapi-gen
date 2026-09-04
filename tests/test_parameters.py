from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from oapi_gen import GenerationError, generate_package
from starlette.applications import Starlette
from starlette.testclient import TestClient

from .support import (
    ApiGenerator,
    PackageImporter,
    SpecWriter,
    make_client,
)


def test_preserves_constraints_and_defaults_from_referenced_parameters(
    tmp_path: Path, write_specification: SpecWriter, import_generated: PackageImporter
) -> None:
    specification = write_specification(
        "referenced-parameter.yaml",
        """
openapi: 3.1.0
info: {title: Referenced parameter, version: 1.0.0}
paths:
  /items:
    get:
      operationId: listItems
      parameters:
        - name: limit
          in: query
          schema: {$ref: '#/components/schemas/PositiveLimit'}
      responses:
        '204': {description: Empty}
components:
  schemas:
    PositiveLimit:
      type: integer
      minimum: 1
      default: 2
""",
    )
    output = tmp_path / "generated_referenced_parameter"
    generate_package(specification, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_referenced_parameter.contracts")
    received_limits: list[int] = []

    class Controller:
        async def list_items(self, request):
            received_limits.append(request.limit)
            return contracts.ListItems.NoContent()

    router = generated.create_router(generated.Handlers(default=Controller()))
    app = Starlette(routes=router.routes)
    client = TestClient(app)

    assert client.get("/items", params={"limit": 0}).status_code == 422
    assert client.get("/items").status_code == 204
    assert received_limits == [2]


def test_scalar_cookie_parameters_keep_referenced_enums(generate_api: ApiGenerator) -> None:
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "parameters": [
                        {
                            "name": "mode",
                            "in": "cookie",
                            "required": True,
                            "schema": {"$ref": "#/components/schemas/Choice"},
                        }
                    ],
                    "responses": {"204": {"description": "Empty"}},
                }
            },
        },
        {"Choice": {"type": "string", "enum": ["one", "two"]}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    models = importlib.import_module(f"{generated.__name__}.models")

    class Controller:
        async def test(self, request):
            assert request.mode == models.Choice.one
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    assert client.get("/value", headers={"Cookie": "mode=one"}).status_code == 204
    assert client.get("/value", headers={"Cookie": "mode=three"}).status_code == 422


@pytest.mark.parametrize("location", ["path", "query", "header"])
def test_array_parameters_decode_and_validate_the_wire_format(
    location: str, generate_api: ApiGenerator
) -> None:
    path = "/values/{ids}" if location == "path" else "/values"
    array = {"type": "array", "minItems": 2, "items": {"$ref": "#/components/schemas/Positive"}}
    generated = generate_api(
        {
            path: {
                "get": {
                    "operationId": "test",
                    "parameters": [
                        {
                            "name": "ids",
                            "in": location,
                            "required": location == "path",
                            "schema": array,
                        }
                    ],
                    "responses": {"204": {"description": "Empty"}},
                }
            },
        },
        {"Positive": {"type": "integer", "minimum": 1}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    received: list[object] = []

    class Controller:
        async def test(self, request):
            received.append(request.ids)
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())

    def request(values: list[str]):
        if location == "path":
            return client.get("/values/" + ",".join(values))
        if location == "header":
            return client.get("/values", headers={"ids": ",".join(values)})
        return client.get("/values", params=[("ids", value) for value in values])

    assert request(["1", "2"]).status_code == 204
    assert received == [[1, 2]]
    for values in [["0", "2"], ["invalid", "2"], ["1"]]:
        response = request(values)
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][:2] == [location, "ids"]
    if location == "header":
        assert client.get("/values", headers=[("ids", "1,2"), ("ids", "3")]).status_code == 204
        assert received[-1] == [1, 2, 3]
    if location != "path":
        assert client.get("/values").status_code == 204
        assert received[-1] is None
    parameter = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"][0]
    wire_schema = parameter["schema"]
    assert wire_schema["type"] == "array"
    assert wire_schema["minItems"] == 2
    assert wire_schema["items"] == {"$ref": "#/components/schemas/Positive"}
    positive = client.get("/openapi.json").json()["components"]["schemas"]["Positive"]
    assert positive == {"type": "integer", "minimum": 1}


@pytest.mark.parametrize(
    ("location", "schema"),
    [
        ("cookie", {"type": "array", "items": {"type": "integer"}}),
        ("query", {"type": "object", "additionalProperties": {"type": "integer"}}),
        ("header", {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}),
    ],
)
def test_unsupported_parameter_shapes_fail_before_writing(
    tmp_path: Path, location: str, schema: dict[str, Any], generate_api: ApiGenerator
) -> None:
    with pytest.raises(GenerationError, match="parameter"):
        generate_api(
            {
                "/value": {
                    "get": {
                        "operationId": "test",
                        "parameters": [{"name": "ids", "in": location, "schema": schema}],
                        "responses": {"204": {"description": "Empty"}},
                    }
                },
            },
        )
    assert not any(path.is_dir() for path in tmp_path.iterdir())


def test_path_names_are_mapped_once_and_errors_keep_wire_names(generate_api):
    path = "/values/{item-id}/{item_id}/{item_id_path}"
    generated = generate_api(
        {
            path: {
                "get": {
                    "operationId": "test",
                    "parameters": [
                        {
                            "in": "path",
                            "name": name,
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                        for name in ("item-id", "item_id", "item_id_path")
                    ],
                    "responses": {"204": {"description": "OK"}},
                }
            }
        }
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    received = []

    class Controller:
        async def test(self, request):
            received.append((request.item_id_path, request.item_id_path_2, request.item_id_path_3))
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    assert client.get("/values/1/2/3").status_code == 204
    assert received == [(1, 2, 3)]
    error = client.get("/values/invalid/2/3")
    assert error.status_code == 422
    assert error.json()["detail"][0]["loc"] == ["path", "item-id"]
    assert path in client.get("/openapi.json").json()["paths"]
