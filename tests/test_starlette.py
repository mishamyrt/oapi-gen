from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import msgspec
import pytest

from oapi_gen import GenerationError, generate_package

from .support import json_response, make_client, request_body


def test_nested_models_aliases_uuid_defaults_and_bad_json(generate_api):
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": request_body({"$ref": "#/components/schemas/Envelope"}),
                    "responses": {"200": json_response({"$ref": "#/components/schemas/Envelope"})},
                }
            }
        },
        {
            "Item": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "display-name"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "display-name": {"type": "string", "minLength": 2},
                    "count": {"type": "integer", "default": 2},
                },
            },
            "Envelope": {
                "type": "object",
                "required": ["items"],
                "properties": {
                    "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                },
            },
        },
    )

    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")

    class Controller:
        async def test(self, request):
            assert isinstance(request.body.items[0], models.Item)
            assert isinstance(request.body.items[0].id, UUID)
            return contracts.Test.Ok(body=request.body)

    client = make_client(generated, Controller())
    body = {"items": [{"id": str(uuid4()), "display-name": "ok"}]}
    response = client.post("/value", json=body)
    assert response.status_code == 200
    assert response.json() == {"items": [{**body["items"][0], "count": 2}]}
    for content in (b"{", b"", b"null"):
        assert (
            client.post(
                "/value", content=content, headers={"content-type": "application/json"}
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/value", content=json.dumps(body), headers={"content-type": "text/plain"}
        ).status_code
        == 422
    )
    body["items"][0]["id"] = "invalid"
    assert client.post("/value", json=body).status_code == 422


def test_parameters_headers_cookies_arrays_and_defaults(generate_api):
    params = [
        {
            "in": "path",
            "name": "ids",
            "schema": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        },
        {
            "in": "query",
            "name": "limit",
            "schema": {"type": "integer", "minimum": 1, "default": 10},
        },
        {
            "in": "query",
            "name": "tag",
            "required": True,
            "schema": {"type": "array", "minItems": 2, "items": {"type": "string"}},
        },
        {
            "in": "header",
            "name": "X-Flags",
            "schema": {"type": "array", "items": {"type": "boolean"}},
        },
        {"in": "cookie", "name": "session", "required": True, "schema": {"type": "string"}},
    ]
    generated = generate_api(
        {
            "/value/{ids}": {
                "get": {
                    "operationId": "test",
                    "parameters": params,
                    "responses": {"204": {"description": "Empty"}},
                }
            }
        }
    )

    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def test(self, request):
            assert (request.ids, request.limit, request.tag, request.x_flags, request.session) == (
                [1, 2],
                10,
                ["a", "b"],
                [True, False, True],
                "ok",
            )
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    headers = [("X-Flags", "on,false"), ("X-Flags", "yes"), ("cookie", "session=ok")]
    assert client.get("/value/1,2?tag=a&tag=b", headers=headers).status_code == 204
    assert client.get("/value/0,2?tag=a&tag=b", headers=headers).status_code == 422
    assert client.get("/value/1,2?tag=a", headers=headers).status_code == 422
    assert client.get("/value/1,2?tag=a&tag=b&limit=0", headers=headers).status_code == 422


def test_import_without_pydantic_fastapi_and_generator(generate_api):
    generated = generate_api({})
    directory = Path(generated.__file__).parent.parent
    script = """
import importlib.abc
import sys
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"fastapi", "pydantic", "oapi_gen"}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Blocker())
module = __import__(sys.argv[1])
module.create_router(module.Handlers())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, generated.__name__],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_checked_responses_normalize_fields_and_validate_existing_structs(generate_api):
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "responses": {"200": json_response({"$ref": "#/components/schemas/Item"})},
                }
            }
        },
        {
            "Item": {
                "type": "object",
                "required": ["count"],
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "label": {"type": "string", "default": "default"},
                },
            }
        },
    )
    returned = {"count": 2, "extra": "must be filtered"}

    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")

    class Controller:
        async def test(self, request):
            return contracts.Test.Ok(body=returned)

    client = make_client(generated, Controller())
    assert client.get("/value").json() == {"count": 2, "label": "default"}
    returned = models.Item(count=0)
    with pytest.raises(msgspec.ValidationError):
        client.get("/value")


def test_unsupported_union_fails_before_writing(generate_api):
    with pytest.raises(GenerationError, match="discriminator"):
        generate_api(
            {
                "/value": {
                    "post": {
                        "operationId": "test",
                        "requestBody": request_body(
                            {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/A"},
                                    {"$ref": "#/components/schemas/B"},
                                ]
                            }
                        ),
                        "responses": {"204": {"description": "Empty"}},
                    }
                }
            },
            {
                name: {
                    "type": "object",
                    "required": [name],
                    "properties": {name: {"type": "string"}},
                }
                for name in ("A", "B")
            },
        )


@pytest.mark.parametrize("fixture", ["cats", "advanced"])
def test_generated_contracts_and_endpoints_typecheck(tmp_path: Path, fixture: str):
    source = Path(__file__).parent / "fixtures" / f"{fixture}.openapi.yaml"
    output = tmp_path / "generated"
    generate_package(source, output)
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "extends": str(Path(__file__).resolve().parents[1] / "pyproject.toml"),
                "reportImportCycles": "error",
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "basedpyright",
            "--project",
            str(config),
            "--pythonpath",
            sys.executable,
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("declare_head", [True, False])
def test_only_declared_http_methods_are_routed(generate_api, declare_head):
    operations = {"get": {"operationId": "getValue", "responses": {"204": {"description": "GET"}}}}
    if declare_head:
        operations["head"] = {
            "operationId": "headValue",
            "responses": {"202": {"description": "HEAD"}},
        }
    generated = generate_api({"/value": operations})

    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def get_value(self, request):
            return contracts.GetValue.NoContent()

        async def head_value(self, request):
            return contracts.HeadValue.Accepted()

    client = make_client(generated, Controller())
    assert client.get("/value").status_code == 204
    assert client.head("/value").status_code == (202 if declare_head else 405)
