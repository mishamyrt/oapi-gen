from __future__ import annotations

import importlib
from pathlib import Path
from uuid import UUID

from starlette.applications import Starlette
from starlette.routing import Router
from starlette.testclient import TestClient

from oapi_gen import generate_package

from .support import (
    ApiGenerator,
    PackageImporter,
    SpecWriter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cats.openapi.yaml"


def test_generates_working_explicitly_bound_router(
    tmp_path: Path, import_generated: PackageImporter
) -> None:
    output = tmp_path / "generated_api"
    generate_package(FIXTURE, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_api.contracts")
    models = importlib.import_module("generated_api.models")

    assert generated.API_INFO_TITLE == "Cats API"
    assert generated.API_INFO_VERSION == "1.0.0"

    cat_id = UUID("6f238436-2994-4afc-a750-7e37be93b16f")

    class CatsController:
        async def list_cats(self, request):
            assert request.limit == 1
            cats = [models.Cat(id=cat_id, name="Mittens")]
            return contracts.ListCats.Ok(body=cats)

        async def create_cat(self, request):
            assert request.body.name == "Mittens"
            return contracts.CreateCat.Created()

        async def show_cat_by_id(self, request):
            if request.cat_id == cat_id:
                return contracts.ShowCatById.Ok(body=models.Cat(id=cat_id, name="Mittens"))
            raise contracts.ShowCatById.NotFound(body=models.HttpError(message="Cat not found"))

    handlers = generated.Handlers(cats=CatsController())
    app = Starlette(routes=generated.create_router(handlers, prefix="/api").routes)
    client = TestClient(app)

    listed = client.get("/api/cats", params={"limit": 1})
    assert listed.status_code == 200
    assert listed.json() == [{"id": str(cat_id), "name": "Mittens"}]

    created = client.post("/api/cats", json={"name": "Mittens"})
    assert created.status_code == 201
    assert created.content == b""

    missing = client.get("/api/cats/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json() == {"message": "Cat not found"}

    operation = client.get("/api/openapi.json").json()["paths"]["/api/cats"]["get"]
    assert operation["operationId"] == "listCats"
    assert operation["parameters"][0]["description"] == "Maximum number of cats"
    assert operation["responses"]["404"]["description"] == "No cats found"


def test_allocates_distinct_names_across_route_argument_categories(
    tmp_path: Path, write_specification: SpecWriter, import_generated: PackageImporter
) -> None:
    specification = write_specification(
        "argument-collisions.yaml",
        """
openapi: 3.1.0
info: {title: Argument collisions, version: 1.0.0}
paths:
  /values:
    post:
      operationId: createValue
      security:
        - apiKey: []
      parameters:
        - {name: foo-bar, in: query, schema: {type: string}}
        - {name: foo_bar, in: query, schema: {type: string}}
        - {name: body_file, in: query, schema: {type: string}}
        - {name: security_api_key, in: query, schema: {type: string}}
        - {name: handlers, in: query, schema: {type: string}}
        - {name: security, in: query, schema: {type: string}}
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              type: object
              required: [file]
              properties:
                file: {$ref: '#/components/schemas/NonEmptyText'}
      responses:
        '204': {description: Created}
components:
  securitySchemes:
    apiKey: {type: apiKey, in: header, name: X-API-Key}
  schemas:
    NonEmptyText: {type: string, minLength: 3}
""",
    )
    output = tmp_path / "generated_argument_collisions"
    generate_package(specification, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_argument_collisions.contracts")

    class Controller:
        async def create_value(self, request):
            assert request.foo_bar_query == "first"
            assert request.foo_bar_query_2 == "second"
            assert request.body_file == "query file"
            assert request.security_api_key == "query key"
            assert request.handlers == "request handlers"
            assert request.security == "request security"
            assert request.body.file == "form file"
            assert request.security_context == {"api_key": "header key"}
            return contracts.CreateValue.NoContent()

    class SecurityController:
        async def handle_api_key(self, context, operation_id, credential):
            assert context is None
            assert operation_id == "createValue"
            return {"api_key": credential.api_key}

    router = generated.create_router(
        generated.Handlers(default=Controller()),
        security=SecurityController(),
    )
    app = Starlette(routes=router.routes)

    client = TestClient(app)
    invalid = client.post(
        "/values",
        headers={"X-API-Key": "header key"},
        files={"file": (None, "x")},
    )
    response = client.post(
        "/values",
        params={
            "foo-bar": "first",
            "foo_bar": "second",
            "body_file": "query file",
            "security_api_key": "query key",
            "handlers": "request handlers",
            "security": "request security",
        },
        headers={"X-API-Key": "header key"},
        files={"file": (None, "form file")},
    )

    assert invalid.status_code == 422
    assert response.status_code == 204


def test_operation_names_do_not_replace_router_internals(generate_api: ApiGenerator) -> None:
    generated = generate_api(
        {
            f"/{index}": {
                "get": {"operationId": name, "responses": {"204": {"description": "Empty"}}}
            }
            for index, name in enumerate(["router", "handlers", "security", "oapiRouter"])
        },
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")

    class Controller:
        async def router(self, request):
            return contracts.Router.NoContent()

        async def handlers(self, request):
            return contracts.HandlersOperation.NoContent()

        async def security(self, request):
            return contracts.Security.NoContent()

        async def oapi_router(self, request):
            return contracts.OapiRouter.NoContent()

    router = generated.create_router(generated.Handlers(default=Controller()))
    assert isinstance(router, Router)
    app = Starlette(routes=router.routes)
    client = TestClient(app)
    for index in range(4):
        assert client.get(f"/{index}").status_code == 204
