from __future__ import annotations

import base64
import importlib
from contextlib import contextmanager
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from typing import Never, get_args, get_type_hints

import pytest
from msgspec import ValidationError

from .support import (
    ApiGenerator,
    json_response,
    make_client,
)


@pytest.mark.parametrize("validate_responses", [True, False])
@pytest.mark.parametrize(
    "media_type,media,version",
    [
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            {"schema": {"type": "string", "format": "binary"}},
            "3.0.4",
        ),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", {}, "3.1.0"),
        ("application/octet-stream", {"schema": {}}, "3.1.0"),
        ("application/pdf", {"schema": {"$ref": "#/components/schemas/File"}}, "3.1.0"),
        ("image/png", {"$ref": "#/components/mediaTypes/File"}, "3.2.0"),
        ("application/json", {"schema": {"type": "string", "format": "binary"}}, "3.1.0"),
    ],
)
def test_file_responses_preserve_bytes_and_headers(
    generate_api: ApiGenerator, validate_responses: bool, media_type: str, media: dict, version: str
) -> None:
    response_spec = {
        "description": "File",
        "content": {media_type: media},
        "headers": {"Content-Disposition": {"required": True, "schema": {"type": "string"}}},
    }
    generated = generate_api(
        {
            "/file": {
                "get": {
                    "operationId": "download",
                    "responses": {"200": response_spec, "400": response_spec},
                }
            }
        },
        {"File": {"type": "string", "format": "binary", "minLength": 4}},
        media_types={"File": {"schema": {"$ref": "#/components/schemas/File"}}},
        openapi_version=version,
        validate_responses=validate_responses,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    assert get_type_hints(contracts.Download.Ok)["body"] is bytes
    content = b"PK\x03\x04\x00\xff\x80"
    disposition = 'attachment; filename="report.xlsx"'
    result = contracts.Download.Ok(body=content, content_disposition=disposition)

    class Controller:
        async def download(self, request):
            if isinstance(result, Exception):
                raise result
            return result

    client = make_client(generated, Controller())
    for variant, status in ((contracts.Download.Ok, 200), (contracts.Download.BadRequest, 400)):
        result = variant(body=content, content_disposition=disposition)
        response = client.get("/file")
        assert response.status_code == status
        if media_type == "application/json":
            assert response.json() == base64.b64encode(content).decode()
        else:
            assert response.content == content
        assert response.headers["content-type"] == media_type
        assert response.headers["content-disposition"] == disposition
        assert response.headers["content-length"] == str(len(response.content))
    assert client.get("/openapi.json").json()["paths"]["/file"]["get"]["responses"]["200"] == (
        response_spec
    )
    if validate_responses:
        invalid_bodies: list[object] = [123]
        if media_type != "application/json":
            invalid_bodies.append(base64.b64encode(content).decode())
        if media_type in {"application/pdf", "image/png"}:
            invalid_bodies.append(b"x")
        for body in invalid_bodies:
            result = contracts.Download.Ok(body=body, content_disposition=disposition)
            with pytest.raises(ValidationError):
                client.get("/file")


@pytest.mark.parametrize("validate_responses", [True, False])
def test_operation_response_namespaces_preserve_variants_and_headers(
    generate_api: ApiGenerator, validate_responses: bool
) -> None:
    success = json_response({"type": "string"})
    success["headers"] = {"X-Request-Id": {"required": True, "schema": {"type": "string"}}}
    generated = generate_api(
        {
            "/login": {
                "post": {
                    "operationId": "login",
                    "responses": {
                        "200": success,
                        "302": {"description": "Redirect"},
                        "401": json_response({"type": "string"}),
                        "422": json_response({"type": "string"}),
                        "499": {"description": "Custom status"},
                    },
                }
            },
            "/logout": {
                "post": {
                    "operationId": "logout",
                    "responses": {"204": {"description": "Logged out"}},
                }
            },
            "/refresh": {
                "post": {
                    "operationId": "refresh",
                    "responses": {"200": success},
                }
            },
            "/blocked": {
                "post": {
                    "operationId": "blocked",
                    "responses": {"503": {"description": "Unavailable"}},
                }
            },
        },
        validate_responses=validate_responses,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    login = contracts.Login
    assert get_args(login.Response.__value__) == (
        login.Ok,
        login.Found,
    )
    assert contracts.Logout.Response.__value__ is contracts.Logout.NoContent
    assert contracts.Blocked.Response.__value__ is Never
    assert not issubclass(login.Ok, Exception)
    assert not issubclass(login.Found, Exception)
    hints = get_type_hints(contracts.DefaultHandler.login)
    assert hints == {"request": login.Request, "return": login.Response}
    result = login.Ok(body="session", x_request_id="request-1")
    return_error = False

    @contextmanager
    def service_scope():
        yield

    def service():
        with service_scope():
            if isinstance(result, BaseException) and not return_error:
                raise result
            return result

    class Controller:
        async def login(self, request):
            assert isinstance(request, login.Request)
            return service()

        async def logout(self, request):
            return contracts.Logout.NoContent()

        async def blocked(self, request):
            raise contracts.Blocked.ServiceUnavailable()

    client = make_client(generated, Controller())
    response = client.post("/login")
    assert response.status_code == 200
    assert response.json() == "session"
    assert response.headers["X-Request-Id"] == "request-1"
    assert client.post("/logout").status_code == 204
    assert client.post("/blocked").status_code == 503
    result = login.Found()
    assert client.post("/login", follow_redirects=False).status_code == 302
    for variant, status in ((login.Unauthorized, 401), (login.UnprocessableEntity, 422)):
        result = variant(body="rejected")
        response = client.post("/login")
        assert response.status_code == status
        assert response.json() == "rejected"
    result = login.Status499()
    response = client.post("/login")
    assert response.status_code == 499
    assert response.content == b""
    return_error = True
    with pytest.raises(TypeError, match="error responses must be raised"):
        client.post("/login")
    return_error = False
    for error in (ValueError("handler failed"), contracts.Blocked.ServiceUnavailable()):
        result = error
        with pytest.raises(type(error)) as caught:
            client.post("/login")
        assert caught.value is error
    result = contracts.Refresh.Ok(body="session", x_request_id="request-1")
    with pytest.raises(TypeError, match="unsupported response variant"):
        client.post("/login")


@pytest.mark.parametrize("validate_responses", [True, False])
@pytest.mark.parametrize("status", [200, 400])
def test_response_aliases_survive_nested_models_arrays_and_headers(
    generate_api: ApiGenerator,
    status: int,
    validate_responses: bool,
) -> None:
    thing = {"$ref": "#/components/schemas/Thing"}
    response = json_response({"type": "array", "items": {"$ref": "#/components/schemas/Envelope"}})
    response["headers"] = {"X-Metadata": {"required": True, "schema": thing}}
    generated = generate_api(
        {
            "/value": {"get": {"operationId": "test", "responses": {str(status): response}}},
        },
        {
            "Thing": {
                "type": "object",
                "required": ["display-name"],
                "properties": {"display-name": {"type": "string"}},
            },
            "Envelope": {
                "type": "object",
                "required": ["nested-value"],
                "properties": {"nested-value": thing},
            },
        },
        validate_responses=validate_responses,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    models = importlib.import_module(f"{generated.__name__}.models")
    value = models.Thing(display_name="value")
    variant = contracts.Test.Ok if status == 200 else contracts.Test.BadRequest

    class Controller:
        async def test(self, request):
            result = variant(
                body=[models.Envelope(nested_value=value)],
                x_metadata=models.Thing(display_name="value"),
            )
            if isinstance(result, Exception):
                raise result
            return result

    client = make_client(generated, Controller())
    response = client.get("/value")
    assert response.status_code == status
    assert response.json() == [{"nested-value": {"display-name": "value"}}]
    assert response.headers["X-Metadata"] == "display-name,value"
    assert (
        "display-name"
        in client.get("/openapi.json").json()["components"]["schemas"]["Thing"]["properties"]
    )
    value.display_name = 42
    if validate_responses:
        with pytest.raises(ValidationError):
            client.get("/value")
    else:
        assert client.get("/value").json() == [{"nested-value": {"display-name": 42}}]


@pytest.mark.parametrize("status", [204, 503])
def test_referenced_response_header_constraints_are_enforced(
    generate_api: ApiGenerator, status: int
) -> None:
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "responses": {
                        str(status): {
                            "description": "Empty",
                            "headers": {
                                "X-Count": {
                                    "required": True,
                                    "schema": {"$ref": "#/components/schemas/Positive"},
                                }
                            },
                        }
                    },
                }
            },
        },
        {"Positive": {"type": "integer", "minimum": 1}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    count = 2
    variant = contracts.Test.NoContent if status == 204 else contracts.Test.ServiceUnavailable

    class Controller:
        async def test(self, request):
            result = variant(x_count=count)
            if isinstance(result, Exception):
                raise result
            return result

    client = make_client(generated, Controller())
    response = client.get("/value")
    assert response.status_code == status
    assert response.headers["X-Count"] == "2"
    count = 0
    with pytest.raises(ValidationError):
        client.get("/value")


def test_error_headers_do_not_shadow_exception_attributes(generate_api: ApiGenerator) -> None:
    response = {
        "description": "Rejected",
        "headers": {
            name: {"required": True, "schema": {"type": "string"}}
            for name in ("Args", "X-Reason", "With-Traceback", "Add-Note")
        },
    }
    generated = generate_api(
        {"/value": {"get": {"operationId": "test", "responses": {"400": response}}}},
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    error = contracts.Test.BadRequest(
        args_header="arguments",
        x_reason="invalid",
        with_traceback_header="trace",
        add_note_header="note",
    )
    assert error.args == ()
    error.add_note("Service rejected the request")
    assert error.with_traceback(None) is error

    class Controller:
        async def test(self, request):
            raise error

    result = make_client(generated, Controller()).get("/value")
    assert result.status_code == 400
    assert {name: result.headers[name] for name in response["headers"]} == {
        "Args": "arguments",
        "X-Reason": "invalid",
        "With-Traceback": "trace",
        "Add-Note": "note",
    }


@pytest.mark.parametrize("validate_responses", [True, False])
@pytest.mark.parametrize("status", [200, 204, 400, 503])
def test_set_cookie_array_preserves_separate_headers(
    generate_api: ApiGenerator, status: int, validate_responses: bool
) -> None:
    has_body = status in (200, 400)
    required = not has_body
    response_spec: dict[str, object] = (
        json_response({"type": "string"}) if has_body else {"description": "Empty"}
    )
    response_spec["headers"] = {
        "set-cookie": {
            "required": required,
            "schema": {"$ref": "#/components/schemas/Cookies"},
        },
        "X-Values": {"schema": {"type": "array", "items": {"type": "string"}}},
    }
    generated = generate_api(
        {"/value": {"get": {"operationId": "test", "responses": {str(status): response_spec}}}},
        {
            "Cookies": {
                "type": "array",
                "minItems": 1 if required else 0,
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 256},
            }
        },
        validate_responses=validate_responses,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    variant = getattr(
        contracts.Test,
        {200: "Ok", 204: "NoContent", 400: "BadRequest", 503: "ServiceUnavailable"}[status],
    )
    expected_type = list[contracts.Cookie] if required else list[contracts.Cookie] | None
    assert get_type_hints(variant)["set_cookie"] == expected_type
    cookies = [
        contracts.Cookie(name="theme", value="dark", httponly=True, secure=True, max_age=3600),
        contracts.Cookie(
            name="language",
            value="ru",
            expires=datetime(2038, 6, 9, 10, 18, 14, tzinfo=UTC),
            path="/auth",
            domain="example.org",
            samesite="strict",
        ),
    ]
    body = {"body": "ok"} if has_body else {}
    result = variant(set_cookie=cookies, x_values=["one", "two"], **body)

    class Controller:
        async def test(self, request):
            if isinstance(result, Exception):
                raise result
            return result

    client = make_client(generated, Controller())
    response = client.get("https://example.org/value")
    assert response.status_code == status
    assert response.headers.get_list("set-cookie") == [
        "theme=dark; HttpOnly; Max-Age=3600; Path=/; SameSite=lax; Secure",
        "language=ru; Domain=example.org; expires=Wed, 09 Jun 2038 10:18:14 GMT; "
        "Path=/auth; SameSite=strict",
    ]
    assert dict(response.cookies) == {"theme": "dark", "language": "ru"}
    assert response.headers.get_list("x-values") == ["one,two"]
    if has_body:
        assert response.json() == "ok"
    else:
        assert response.content == b""

    for empty in ([], None):
        result = variant(set_cookie=empty, **body)
        if required and validate_responses:
            with pytest.raises(ValidationError):
                client.get("/value")
        else:
            assert "set-cookie" not in client.get("/value").headers

    if validate_responses:
        for invalid in (
            ["theme=dark"],
            "theme=dark",
            [contracts.Cookie(name="theme", value=42)],
            [contracts.Cookie(name="theme", samesite="invalid")],
            [contracts.Cookie(name="theme", max_age="3600")],
            [contracts.Cookie(name="theme", secure="yes")],
            [contracts.Cookie(name="theme", value="x" * 300)],
            [contracts.Cookie(name="theme")] * 3,
        ):
            result = variant(set_cookie=invalid, **body)
            with pytest.raises(ValidationError):
                client.get("/value")

    result = variant(
        set_cookie=[contracts.Cookie(name="session", max_age=0, path=None, samesite=None)], **body
    )
    assert client.get("/value").headers.get_list("set-cookie") == ['session=""; Max-Age=0']

    result = variant(set_cookie=[contracts.Cookie(name="unsafe name")], **body)
    with pytest.raises(CookieError):
        client.get("/value")

    result = variant(set_cookie=[contracts.Cookie(name="quoted", value='a;b,"c')], **body)
    encoded = client.get("/value").headers.get_list("set-cookie")
    assert len(encoded) == 1
    parsed = SimpleCookie()
    parsed.load(encoded[0])
    assert parsed["quoted"].value == 'a;b,"c'


@pytest.mark.parametrize("validate_responses", [True, False])
def test_set_cookie_string_remains_a_single_header(
    generate_api: ApiGenerator, validate_responses: bool
) -> None:
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "responses": {
                        "204": {
                            "description": "Empty",
                            "headers": {"Set-Cookie": {"schema": {"type": "string"}}},
                        }
                    },
                }
            }
        },
        validate_responses=validate_responses,
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    cookie = "language=ru; Expires=Wed, 09 Jun 2038 10:18:14 GMT"

    class Controller:
        async def test(self, request):
            return contracts.Test.NoContent(set_cookie=cookie)

    response = make_client(generated, Controller()).get("/value")
    assert response.headers.get_list("set-cookie") == [cookie]
