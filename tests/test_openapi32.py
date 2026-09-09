from __future__ import annotations

import importlib

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oapi_gen import GenerationError
from oapi_gen.parser import OpenAPIParser

from .support import make_client


def document(response=None, **fields):
    return {
        "openapi": "3.2.0",
        "info": {"title": "OpenAPI 3.2", "version": "1"},
        "paths": {
            "/value": {"get": {"operationId": "getValue", "responses": {"200": response or {}}}}
        },
        **fields,
    }


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"summary": "Short"},
        {"description": "Long"},
        {"summary": "Short", "description": "Long"},
    ],
)
def test_response_metadata_accepts_optional_description(generate_api, response):
    generated = generate_api(document(response)["paths"], openapi_version="3.2.0")
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    for text in response.values():
        assert text in contracts.GetValue.Ok.__doc__

    class Controller:
        async def get_value(self, request):
            return contracts.GetValue.Ok()

    client = make_client(generated, Controller())
    assert client.get("/value").content == b""
    assert (
        client.get("/openapi.json").json()["paths"]["/value"]["get"]["responses"]["200"] == response
    )


@pytest.mark.parametrize("version", ["3.0.4", "3.1.1"])
def test_old_versions_still_require_response_description(version):
    with pytest.raises(GenerationError, match="description"):
        OpenAPIParser(document(openapi=version), "hash").parse()


@pytest.mark.parametrize("media_type", ["application/json", "multipart/form-data"])
def test_request_and_response_media_type_references(generate_api, media_type):
    payload_schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }
    body_schema = (
        {"$ref": "#/components/schemas/Payload"}
        if media_type == "application/json"
        else payload_schema
    )
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "value",
                    "requestBody": {
                        "required": True,
                        "content": {media_type: {"$ref": "#/components/mediaTypes/Input"}},
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"$ref": "#/components/mediaTypes/Output"}
                            }
                        }
                    },
                }
            }
        },
        {"Payload": payload_schema},
        media_types={"Input": {"schema": body_schema}, "Output": {"schema": {"type": "integer"}}},
        openapi_version="3.2.0",
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def value(self, request):
            return contracts.Value.Ok(body=request.body.value)

    client = make_client(generated, Controller())
    response = (
        client.post("/value", json={"value": 12})
        if media_type == "application/json"
        else client.post("/value", files={"value": (None, "12")})
    )
    assert response.status_code == 200
    assert response.json() == 12


@pytest.mark.parametrize(
    "reference,media_types,message",
    [
        ("#/components/mediaTypes/Missing", {}, "unresolved"),
        ("other.yaml#/Media", {}, "external"),
        (
            "#/components/mediaTypes/A",
            {
                "A": {"$ref": "#/components/mediaTypes/B"},
                "B": {"$ref": "#/components/mediaTypes/A"},
            },
            "cyclic",
        ),
    ],
)
def test_bad_media_type_references_fail_with_context(reference, media_types, message):
    raw = document(
        {"content": {"application/json": {"$ref": reference}}},
        components={"mediaTypes": media_types},
    )
    with pytest.raises(GenerationError, match=message) as caught:
        OpenAPIParser(raw, "hash").parse()
    assert "GET /value.responses.200" in str(caught.value)


@pytest.mark.parametrize("keyword", ["query", "additionalOperations"])
def test_new_unsupported_http_operations_are_not_silently_skipped(keyword):
    raw = document()
    raw["paths"]["/value"][keyword] = {}
    with pytest.raises(GenerationError, match=keyword):
        OpenAPIParser(raw, "hash").parse()


@pytest.mark.parametrize(
    "media_type,media,message",
    [
        ("application/jsonl", {}, "itemSchema"),
        (
            "application/jsonl",
            {"schema": {"type": "array"}, "itemSchema": {"type": "string"}},
            "whole-stream",
        ),
        (
            "application/json",
            {"schema": {"type": "string"}, "itemSchema": {"type": "string"}},
            "sequential",
        ),
        (
            "application/jsonl",
            {"itemSchema": {"type": "string"}, "itemEncoding": {}},
            "itemEncoding",
        ),
        ("text/event-stream", {"itemSchema": {"type": "string"}}, "flat object"),
        (
            "text/event-stream",
            {"itemSchema": {"type": "object", "properties": {"data": {"type": "string"}}}},
            "require data",
        ),
        (
            "text/event-stream",
            {
                "itemSchema": {
                    "type": "object",
                    "required": ["data"],
                    "properties": {"data": {"type": "object"}},
                }
            },
            "type string",
        ),
    ],
)
def test_unsupported_stream_descriptions_fail_at_generation(media_type, media, message):
    with pytest.raises(GenerationError, match=message):
        OpenAPIParser(document({"content": {media_type: media}}), "hash").parse()


def device_scheme():
    return {
        "type": "oauth2",
        "deprecated": True,
        "oauth2MetadataUrl": "https://example.com/.well-known/oauth-authorization-server",
        "flows": {
            "deviceAuthorization": {
                "deviceAuthorizationUrl": "https://example.com/device",
                "tokenUrl": "https://example.com/token",
                "scopes": {"read": "Read data"},
            }
        },
    }


def test_device_authorization_metadata_and_deprecation_preserve_bearer_contract(generate_api):
    scheme = device_scheme()
    paths = {
        "/value": {
            "get": {
                "operationId": "value",
                "security": [{"device": ["read"]}],
                "responses": {"204": {}},
            }
        }
    }
    parsed = OpenAPIParser(
        document(paths=paths, components={"securitySchemes": {"device": scheme}}), "hash"
    ).parse()
    assert parsed.security_schemes[0].flows == scheme["flows"]
    assert parsed.security_schemes[0].oauth2_metadata_url == scheme["oauth2MetadataUrl"]
    assert parsed.security_schemes[0].deprecated is True
    generated = generate_api(paths, security_schemes={"device": scheme}, openapi_version="3.2.0")
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    assert "Deprecated" in contracts.DeviceSecurity.__doc__
    assert scheme["oauth2MetadataUrl"] in contracts.DeviceSecurity.__doc__

    class Security:
        async def handle_device(self, context, operation_id, credential):
            assert credential.token == "token"
            assert credential.scopes == ("read",)
            return "user"

    class Controller:
        async def value(self, request):
            assert request.security_context == "user"
            return contracts.Value.NoContent()

    client = TestClient(
        Starlette(
            routes=generated.create_router(
                generated.Handlers(default=Controller()), security=Security()
            ).routes
        )
    )
    assert client.get("/value").status_code == 401
    assert client.get("/value", headers={"Authorization": "Bearer token"}).status_code == 204
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["device"] == scheme


@pytest.mark.parametrize("missing", ["deviceAuthorizationUrl", "tokenUrl", "scopes"])
def test_device_authorization_requires_endpoints_and_scopes(missing):
    scheme = device_scheme()
    del scheme["flows"]["deviceAuthorization"][missing]
    with pytest.raises(GenerationError, match=missing):
        OpenAPIParser(document(components={"securitySchemes": {"device": scheme}}), "hash").parse()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("oauth2MetadataUrl", "http://example.com/metadata", "HTTPS"),
        ("oauth2MetadataUrl", 1, "string"),
        ("deprecated", "yes", "boolean"),
    ],
)
def test_security_metadata_is_validated(field, value, message):
    scheme = device_scheme()
    scheme[field] = value
    with pytest.raises(GenerationError, match=message):
        OpenAPIParser(document(components={"securitySchemes": {"device": scheme}}), "hash").parse()
