from __future__ import annotations

import importlib

import pytest
from starlette.datastructures import UploadFile

from oapi_gen import GenerationError

from .support import (
    ApiGenerator,
    make_client,
)


def test_optional_multipart_validates_required_fields_without_security(
    generate_api: ApiGenerator,
) -> None:
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["first", "second"],
                                    "properties": {
                                        "first": {"type": "string"},
                                        "second": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"204": {"description": "Empty"}},
                }
            },
        },
    )
    contracts = importlib.import_module(f"{generated.__name__}.contracts")
    received: list[object] = []

    class Controller:
        async def test(self, request):
            received.append(request.body)
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    assert client.post("/value").status_code == 204
    assert received == [None]
    response = client.post("/value", files={"first": (None, "one")})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "second"]
    assert (
        client.post("/value", files={"first": (None, "one"), "second": (None, "two")}).status_code
        == 204
    )
    assert len(received) == 2


@pytest.mark.parametrize("required", [False, True])
def test_multipart_body_presence_is_independent_of_required_fields(required, generate_api):
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": {
                        "required": required,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                }
                            }
                        },
                    },
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
    expected = 422 if required else 204
    assert client.post("/value").status_code == expected
    headers = {"content-type": "multipart/form-data; boundary=probe"}
    assert client.post("/value", headers=headers, content=b"").status_code == expected
    assert client.post("/value", headers=headers, content=iter([b""])).status_code == expected
    # An actual multipart body may represent an empty object when every field is optional.
    assert client.post("/value", headers=headers, content=b"--probe--\r\n").status_code == 204
    assert received[-1].value is None
    assert client.post("/value", data={"value": "wrong-media"}).status_code == 422
    assert client.post("/value", files={"value": (None, "ok")}).status_code == 204
    assert received[-1].value == "ok"


@pytest.mark.parametrize("field_required", [False, True])
def test_file_array_bounds_and_file_cleanup(field_required, generate_api, monkeypatch):
    closed = []
    original_close = UploadFile.close

    async def close(upload):
        await original_close(upload)
        closed.append(upload.file.closed)

    monkeypatch.setattr(UploadFile, "close", close)
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["files"] if field_required else [],
                                    "properties": {
                                        "files": {
                                            "type": "array",
                                            "minItems": 2,
                                            "maxItems": 2,
                                            "items": {"type": "string", "format": "binary"},
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "204": {"description": "OK"},
                        "400": {"description": "Rejected"},
                    },
                }
            }
        }
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    fail = False
    reject = False

    class Controller:
        async def test(self, request):
            assert len(request.body.files) == 2
            if fail:
                raise RuntimeError("handler failed")
            if reject:
                raise contracts.Test.BadRequest()
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    for count, expected in [(1, 422), (2, 204), (3, 422)]:
        closed.clear()
        files = [("files", (f"{i}.txt", b"ok")) for i in range(count)]
        result = client.post("/value", files=files)
        assert result.status_code == expected
        assert closed == [True] * count
    reject = True
    closed.clear()
    response = client.post("/value", files=[("files", ("a", b"x")), ("files", ("b", b"y"))])
    assert response.status_code == 400
    assert closed == [True, True]
    fail = True
    closed.clear()
    with pytest.raises(RuntimeError, match="handler failed"):
        client.post("/value", files=[("files", ("a", b"x")), ("files", ("b", b"y"))])
    assert closed == [True, True]


@pytest.mark.parametrize("referenced", [False, True])
def test_multipart_root_constraints_fail_before_writing(referenced, generate_api, tmp_path):
    schema = {"type": "object", "not": {}, "properties": {"value": {"type": "string"}}}
    with pytest.raises(GenerationError, match="not"):
        generate_api(
            {
                "/value": {
                    "post": {
                        "operationId": "test",
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {"$ref": "#/components/schemas/Value"}
                                    if referenced
                                    else schema
                                }
                            }
                        },
                        "responses": {"204": {"description": "OK"}},
                    }
                }
            },
            {"Value": schema} if referenced else None,
        )
    assert not any(path.is_dir() for path in tmp_path.iterdir())


@pytest.mark.parametrize("referenced", [False, True])
def test_multipart_property_counts_use_unique_field_names(referenced, generate_api):
    schema = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 2,
        "properties": {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}},
    }
    generated = generate_api(
        {
            "/value": {
                "post": {
                    "operationId": "test",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "multipart/form-data": {
                                "schema": {"$ref": "#/components/schemas/Value"}
                                if referenced
                                else schema
                            },
                        },
                    },
                    "responses": {"204": {"description": "OK"}},
                }
            }
        },
        {"Value": schema} if referenced else None,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def test(self, request):
            return contracts.Test.NoContent()

    client = make_client(generated, Controller())
    assert client.post("/value").status_code == 204
    assert (
        client.post(
            "/value",
            content=b"--probe--\r\n",
            headers={"content-type": "multipart/form-data; boundary=probe"},
        ).status_code
        == 422
    )
    files: list[tuple[str, tuple[str | None, bytes | str]]] = [
        ("files", ("a.txt", b"a")),
        ("files", ("b.txt", b"b")),
    ]
    assert client.post("/value", files=files).status_code == 204
    files.append(("extra", (None, "value")))
    assert client.post("/value", files=files).status_code == 204
    files.append(("third", (None, "value")))
    assert client.post("/value", files=files).status_code == 422
