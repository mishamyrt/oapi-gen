from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest
from oapi_gen import GenerationError, generate_package
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from .support import (
    ApiGenerator,
    PackageImporter,
    SpecWriter,
)

SECURITY_FIXTURE = Path(__file__).parent / "fixtures" / "security.openapi.yaml"


def test_security_group_preserves_authorization_protocol(generate_api: ApiGenerator) -> None:
    generated = generate_api(
        {
            "/security": {
                "get": {
                    "operationId": "getSecurity",
                    "tags": ["security"],
                    "security": [{"key": []}],
                    "responses": {"204": {"description": "Empty"}},
                }
            }
        },
        security_schemes={"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")

    assert hasattr(contracts.SecurityHandler, "handle_key")
    assert hasattr(contracts.SecurityHandler_2, "get_security")
    assert contracts.SecurityHandler is not contracts.SecurityHandler_2
    assert get_type_hints(generated.Handlers)["security"] is contracts.SecurityHandler_2
    assert get_type_hints(generated.create_router)["security"] is contracts.SecurityHandler


def test_supports_security_multipart_and_response_headers(
    tmp_path: Path,
    import_generated: PackageImporter,
) -> None:
    output = tmp_path / "generated_advanced_api"
    generate_package(SECURITY_FIXTURE, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_advanced_api.contracts")
    models = importlib.import_module("generated_advanced_api.models")
    request_id = UUID("91c17b5f-f7d6-47e6-aaf0-cb0da14997ec")
    security_calls: list[tuple[str, str, tuple[str, ...]]] = []

    class UploadsController:
        async def create_upload(self, request):
            assert request.security_context == {"subject": "user"}
            assert request.body.title == "report"
            assert request.body.public is True
            assert request.body.file.filename == "report.txt"
            contents = await request.body.file.read()
            return contracts.CreateUpload.Created(
                body=models.Upload(title=request.body.title, size=len(contents)),
                x_request_id=request_id,
                x_rate_limit=12,
            )

        async def get_admin(self, request):
            assert request.security_context == {"api_key": True, "subject": "user"}
            return contracts.GetAdmin.NoContent()

        async def optional_upload(self, request):
            assert request.body is None
            return contracts.OptionalUpload.NoContent()

        async def get_http_auth(self, request):
            assert request.security_context in ({"basic": True}, {"subject": "user"})
            return contracts.GetHttpAuth.NoContent()

    class SecurityController:
        async def handle_api_key(self, context, operation_id, credential):
            assert credential.api_key == "secret"
            security_calls.append((operation_id, "apiKey", credential.roles))
            return {**(context or {}), "api_key": True}

        async def handle_oauth(self, context, operation_id, credential):
            assert credential.token == "token"
            security_calls.append((operation_id, "oauth", credential.scopes))
            return {**(context or {}), "subject": "user"}

        async def handle_basic_auth(self, context, operation_id, credential):
            assert (credential.username, credential.password) == ("user", "password")
            security_calls.append((operation_id, "basicAuth", credential.roles))
            return {**(context or {}), "basic": True}

        async def handle_bearer_auth(self, context, operation_id, credential):
            assert credential.token == "token"
            security_calls.append((operation_id, "bearerAuth", credential.roles))
            return {**(context or {}), "subject": "user"}

    handlers = generated.Handlers(uploads=UploadsController())
    router = generated.create_router(handlers, security=SecurityController(), prefix="/api")
    app = Starlette(routes=router.routes)
    client = TestClient(app)

    uploaded = client.post(
        "/api/uploads",
        headers={"Authorization": "Bearer token"},
        files={"file": ("report.txt", b"hello", "text/plain")},
        data={"title": "report", "public": "true"},
    )
    assert uploaded.status_code == 201
    assert uploaded.json() == {"title": "report", "size": 5}
    assert uploaded.headers["X-Request-ID"] == str(request_id)
    assert uploaded.headers["X-Rate-Limit"] == "12"
    assert security_calls == [("createUpload", "oauth", ("uploads:write",))]

    security_calls.clear()
    uploaded_with_both_credentials = client.post(
        "/api/uploads",
        headers={"Authorization": "Bearer token", "X-API-Key": "secret"},
        files={"file": ("report.txt", b"hello", "text/plain")},
        data={"title": "report", "public": "true"},
    )
    assert uploaded_with_both_credentials.status_code == 201
    assert security_calls == [("createUpload", "oauth", ("uploads:write",))]

    optional = client.post("/api/optional-upload")
    assert optional.status_code == 204

    basic = client.get("/api/http-auth", auth=("user", "password"))
    assert basic.status_code == 204

    bearer = client.get("/api/http-auth", headers={"Authorization": "Bearer token"})
    assert bearer.status_code == 204

    denied = client.get("/api/admin", headers={"Authorization": "Bearer token"})
    assert denied.status_code == 401

    allowed = client.get(
        "/api/admin",
        headers={"Authorization": "Bearer token", "X-API-Key": "secret"},
    )
    assert allowed.status_code == 204

    schema = client.get("/api/openapi.json").json()
    operation = schema["paths"]["/api/admin"]["get"]
    assert operation["security"] == [{"apiKey": [], "oauth": ["admin:read"]}]
    schemes = schema["components"]["securitySchemes"]
    assert schemes["apiKey"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    assert schemes["oauth"]["type"] == "oauth2"
    upload_operation = schema["paths"]["/api/uploads"]["post"]
    upload_response = upload_operation["responses"]["201"]
    header = upload_response["headers"]["X-Request-ID"]
    if "$ref" in header:
        header = schema["components"]["headers"]["RequestID"]
    assert header["required"] is True
    assert upload_operation["requestBody"]["description"] == "File and metadata"
    encoding = upload_operation["requestBody"]["content"]["multipart/form-data"]["encoding"]
    assert encoding["file"]["contentType"] == "application/octet-stream"


def test_rejects_unknown_security_scheme(tmp_path: Path) -> None:
    specification = tmp_path / "secure.yaml"
    specification.write_text(
        """
openapi: 3.1.0
info: {title: Secure, version: 1.0.0}
security: [{missing: []}]
paths:
  /items:
    get:
      operationId: listItems
      responses:
        '204': {description: Empty}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(GenerationError, match="unknown security scheme"):
        generate_package(specification, tmp_path / "generated")


def test_evaluates_security_alternatives_with_their_own_scopes(
    tmp_path: Path, write_specification: SpecWriter, import_generated: PackageImporter
) -> None:
    specification = write_specification(
        "security-alternatives.yaml",
        """
openapi: 3.1.0
info: {title: Security alternatives, version: 1.0.0}
paths:
  /items:
    get:
      operationId: listItems
      security:
        - oauth: [items:read]
        - oauth: [items:write]
      responses:
        '204': {description: Allowed}
components:
  securitySchemes:
    oauth:
      type: oauth2
      flows:
        clientCredentials:
          tokenUrl: https://example.com/oauth/token
          scopes:
            items:read: Read items
            items:write: Write items
""",
    )
    output = tmp_path / "generated_security_alternatives"
    generate_package(specification, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_security_alternatives.contracts")
    security_calls: list[tuple[str, ...]] = []

    class ItemsController:
        async def list_items(self, request):
            assert request.security_context == {"scope": "items:write"}
            return contracts.ListItems.NoContent()

    class SecurityController:
        async def handle_oauth(self, context, operation_id, credential):
            assert context is None
            assert operation_id == "listItems"
            security_calls.append(credential.scopes)
            if credential.scopes == ("items:read",):
                raise generated.SecurityRejected
            return {"scope": credential.scopes[0]}

    router = generated.create_router(
        generated.Handlers(default=ItemsController()),
        security=SecurityController(),
    )
    app = Starlette(routes=router.routes)

    response = TestClient(app).get("/items", headers={"Authorization": "Bearer token"})

    assert response.status_code == 204
    assert security_calls == [("items:read",), ("items:write",)]


def test_malformed_basic_does_not_abort_other_alternatives(generate_api):
    schemes = {
        "key": {"type": "apiKey", "in": "header", "name": "X-Key"},
        "basic": {"type": "http", "scheme": "basic"},
    }
    generated = generate_api(
        {
            "/private": {
                "get": {
                    "operationId": "private",
                    "security": [{"key": []}, {"basic": []}],
                    "responses": {"204": {"description": "OK"}},
                }
            },
            "/optional": {
                "get": {
                    "operationId": "optional",
                    "security": [{"basic": []}, {}],
                    "responses": {"204": {"description": "OK"}},
                }
            },
        },
        security_schemes=schemes,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    calls = []

    class Security:
        async def handle_key(self, context, operation_id, credential):
            calls.append("key")
            if credential.api_key == "abort":
                raise HTTPException(403, "denied")
            return "user"

        async def handle_basic(self, context, operation_id, credential):
            calls.append("basic")
            return "user"

    class Controller:
        async def private(self, request):
            assert request.security_context == "user"
            return contracts.Private.NoContent()

        async def optional(self, request):
            assert request.security_context is None
            return contracts.Optional.NoContent()

    client = TestClient(
        Starlette(
            routes=generated.create_router(
                generated.Handlers(default=Controller()), security=Security()
            ).routes
        )
    )
    assert (
        client.get(
            "/private", headers={"X-Key": "valid", "Authorization": "Basic invalid"}
        ).status_code
        == 204
    )
    assert calls == ["key"]
    assert client.get("/private", headers={"Authorization": "Basic invalid"}).status_code == 401
    assert client.get("/optional", headers={"Authorization": "Basic invalid"}).status_code == 204
    assert (
        client.get("/private", headers={"X-Key": "abort"}, auth=("user", "pass")).status_code == 403
    )
    assert calls == ["key", "key"]


@pytest.mark.parametrize("media_type", ["application/json", "multipart/form-data"])
def test_authorization_rejects_before_reading_any_body(media_type, generate_api, monkeypatch):
    schema = (
        {"type": "string"}
        if media_type == "application/json"
        else {"type": "object", "properties": {"value": {"type": "string"}}}
    )
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "security": [{"key": []}],
                    "requestBody": {"required": True, "content": {media_type: {"schema": schema}}},
                    "responses": {"204": {"description": "OK"}},
                }
            }
        },
        security_schemes={"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
    )

    class Security:
        async def handle_key(self, context, operation_id, credential):
            raise generated.SecurityRejected()

    class Controller:
        async def test(self, request):
            raise AssertionError("unauthorized handler was called")

    def unexpected_read(*args, **kwargs):
        raise AssertionError("unauthorized request body was read")

    monkeypatch.setattr(Request, "body", unexpected_read)
    monkeypatch.setattr(Request, "stream", unexpected_read)
    client = TestClient(
        Starlette(
            routes=generated.create_router(
                generated.Handlers(default=Controller()), security=Security()
            ).routes
        )
    )
    assert (
        client.post(
            "/value", content=b"unread", headers={"content-type": media_type, "X-Key": "rejected"}
        ).status_code
        == 401
    )
