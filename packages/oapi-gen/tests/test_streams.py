from __future__ import annotations

import importlib
from typing import get_type_hints

import anyio
import msgspec
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from .support import json_response, make_client


def stream_operation(media_type, item_schema, **fields):
    return {
        "/events": {
            "get": {
                "operationId": "watch",
                "responses": {"200": {"content": {media_type: {"itemSchema": item_schema}}}},
                **fields,
            }
        }
    }


def event_schema(data=None):
    return {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": data or {"type": "string"},
            "event": {"type": "string"},
            "id": {"type": "string"},
            "retry": {"type": "integer", "minimum": 0},
        },
    }


def asgi_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "headers": [],
    }


@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.parametrize(
    "media_type", ["application/jsonl", "application/x-ndjson", "application/json-seq"]
)
def test_json_streams_encode_each_typed_item(generate_api, media_type, checked):
    generated = generate_api(
        stream_operation(media_type, {"$ref": "#/components/schemas/Item"}),
        {
            "Item": {
                "type": "object",
                "required": ["wire-name"],
                "properties": {"wire-name": {"type": "string"}},
            }
        },
        openapi_version="3.2.0",
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")
    closed = []

    class Controller:
        async def watch(self, request):
            async def items():
                try:
                    yield models.Item(wire_name="Кот\nCat")
                    yield models.Item(wire_name="Dog")
                finally:
                    closed.append(True)

            return contracts.Watch.Ok(body=items())

    response = make_client(generated, Controller()).get("/events")
    prefix = b"\x1e" if media_type == "application/json-seq" else b""
    assert response.content == b"".join(
        prefix + msgspec.json.encode({"wire-name": name}) + b"\n" for name in ("Кот\nCat", "Dog")
    )
    assert response.headers["content-type"] == media_type
    assert "content-length" not in response.headers
    assert closed == [True]


@pytest.mark.parametrize("checked", [True, False])
def test_sse_typed_json_uses_media_reference_and_preserves_wire_schema(generate_api, checked):
    schema = event_schema(
        {
            "type": "string",
            "contentMediaType": "application/json",
            "contentSchema": {"$ref": "#/components/schemas/Cat"},
        }
    )
    schema["properties"]["event"]["const"] = "cat.updated"
    generated = generate_api(
        {
            "/events": {
                "get": {
                    "operationId": "watch",
                    "responses": {
                        "200": {
                            "summary": "Cat changes",
                            "description": "One cat per event.",
                            "headers": {
                                "X-Request-Id": {"required": True, "schema": {"type": "string"}}
                            },
                            "content": {
                                "text/event-stream": {"$ref": "#/components/mediaTypes/Cats"}
                            },
                        }
                    },
                }
            }
        },
        {
            "Cat": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
            "CatEvent": schema,
        },
        media_types={
            "Cats": {"$ref": "#/components/mediaTypes/Base"},
            "Base": {"itemSchema": {"$ref": "#/components/schemas/CatEvent"}},
        },
        openapi_version="3.2.0",
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")
    assert get_type_hints(contracts.Watch.Event)["data"] is models.Cat
    assert get_type_hints(models.CatEvent)["data"] is str
    assert contracts.Watch.Ok.__doc__.startswith("Cat changes\n\nOne cat per event.")

    class Controller:
        async def watch(self, request):
            async def events():
                yield contracts.Watch.Event(
                    data=models.Cat(name="Mittens"), event="cat.updated", id="1", retry=0
                )

            return contracts.Watch.Ok(body=events(), x_request_id="request-1")

    client = make_client(generated, Controller())
    response = client.get("/events")
    assert response.content == b'event: cat.updated\nid: 1\nretry: 0\ndata: {"name":"Mittens"}\n\n'
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-request-id"] == "request-1"
    document = client.get("/openapi.json").json()
    assert document["components"]["schemas"]["CatEvent"] == schema
    assert document["paths"]["/events"]["get"]["responses"]["200"]["content"][
        "text/event-stream"
    ] == {"$ref": "#/components/mediaTypes/Cats"}


def test_sse_text_preserves_multiline_empty_and_unicode_data(generate_api):
    generated = generate_api(
        stream_operation("text/event-stream", event_schema()), openapi_version="3.2.0"
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def watch(self, request):
            async def events():
                yield contracts.Watch.Event(data="Кот\r\nDog\rBird\n", id="")
                yield contracts.Watch.Event(data="")

            return contracts.Watch.Ok(body=events())

    response = make_client(generated, Controller()).get("/events")
    assert (
        response.content.decode() == "id: \ndata: Кот\ndata: Dog\ndata: Bird\ndata: \n\ndata: \n\n"
    )


@pytest.mark.parametrize("media_type", ["text/event-stream", "application/x-ndjson"])
def test_stream_sends_before_completion_and_closes_on_disconnect(generate_api, media_type):
    schema = event_schema() if media_type == "text/event-stream" else {"type": "string"}
    generated = generate_api(stream_operation(media_type, schema), openapi_version="3.2.0")
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    closed = []

    class Controller:
        async def watch(self, request):
            async def events():
                try:
                    yield (
                        contracts.Watch.Event(data="first")
                        if media_type == "text/event-stream"
                        else "first"
                    )
                    await anyio.sleep_forever()
                    raise AssertionError("stream was eagerly consumed")
                finally:
                    # Awaited cleanup in user code follows AnyIO cancellation rules.
                    with anyio.CancelScope(shield=True):
                        await anyio.sleep(0)
                        closed.append(True)

            return contracts.Watch.Ok(body=events())

    app = Starlette(routes=generated.create_router(generated.Handlers(default=Controller())).routes)

    async def exercise():
        delivered = anyio.Event()
        messages = []

        async def receive():
            await delivered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                assert message["more_body"] is True
                delivered.set()

        with anyio.fail_after(2):
            await app(asgi_scope(), receive, send)
        assert messages[0]["type"] == "http.response.start"
        assert len(messages) == 2

    anyio.run(exercise)
    assert closed == [True]


def test_heartbeat_keeps_idle_sse_alive_and_disconnect_cancels_source(generate_api, monkeypatch):
    generated = generate_api(
        stream_operation("text/event-stream", event_schema()), openapi_version="3.2.0"
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    streams = importlib.import_module(generated.__name__ + "._streams")
    monkeypatch.setattr(streams, "_HEARTBEAT_SECONDS", 0.01)
    closed = []

    class Controller:
        async def watch(self, request):
            async def events():
                try:
                    await anyio.sleep_forever()
                    yield contracts.Watch.Event(data="unreachable")
                finally:
                    closed.append(True)

            return contracts.Watch.Ok(body=events())

    app = Starlette(routes=generated.create_router(generated.Handlers(default=Controller())).routes)

    async def exercise():
        heartbeat = anyio.Event()

        async def receive():
            await heartbeat.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                assert message["body"].startswith(b":")
                heartbeat.set()

        with anyio.fail_after(2):
            await app(asgi_scope(), receive, send)

    anyio.run(exercise)
    assert closed == [True]


@pytest.mark.parametrize("checked", [True, False])
def test_validation_is_per_item_and_http_errors_only_precede_stream(generate_api, checked):
    generated = generate_api(
        stream_operation(
            "application/jsonl",
            {"type": "integer", "minimum": 1},
            responses={
                "200": {
                    "content": {
                        "application/jsonl": {"itemSchema": {"type": "integer", "minimum": 1}}
                    }
                },
                "403": json_response({"type": "string"}),
            },
        ),
        openapi_version="3.2.0",
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    closed = []

    class Controller:
        denied = False

        async def watch(self, request):
            if self.denied:
                raise contracts.Watch.Forbidden(body="denied")

            async def events():
                try:
                    yield 1
                    yield 0
                finally:
                    closed.append(True)

            return contracts.Watch.Ok(body=events())

    controller = Controller()
    app = Starlette(routes=generated.create_router(generated.Handlers(default=controller)).routes)

    async def exercise():
        messages = []

        async def receive():
            await anyio.sleep_forever()
            raise AssertionError("receive unexpectedly resumed")

        async def send(message):
            messages.append(message)

        if checked:
            with pytest.raises(msgspec.ValidationError):
                await app(asgi_scope(), receive, send)
            assert [message.get("body") for message in messages] == [None, b"1\n"]
        else:
            await app(asgi_scope(), receive, send)
            assert [message.get("body") for message in messages] == [None, b"1\n", b"0\n", b""]
        assert messages[0]["status"] == 200

    anyio.run(exercise)
    assert closed == [True]
    controller.denied = True
    response = TestClient(app).get("/events")
    assert response.status_code == 403
    assert response.json() == "denied"


@pytest.mark.parametrize("checked", [True, False])
def test_sse_framing_is_enforced_even_without_schema_validation(generate_api, checked):
    generated = generate_api(
        stream_operation("text/event-stream", event_schema()),
        openapi_version="3.2.0",
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        item: object = None

        async def watch(self, request):
            async def events():
                yield self.item

            return contracts.Watch.Ok(body=events())

    controller = Controller()
    client = make_client(generated, controller)
    for field, value in (
        ("event", "bad\nevent"),
        ("id", "bad\rid"),
        ("id", "bad\0id"),
        ("retry", -1),
    ):
        controller.item = contracts.Watch.Event(data="ok", **{field: value})
        with pytest.raises((ValueError, msgspec.ValidationError)):
            client.get("/events")
    controller.item = {"data": "not a generated event"}
    with pytest.raises(TypeError, match=r"requires.*Event"):
        client.get("/events")


@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.parametrize(
    "data_schema,value",
    [
        ({"type": "string", "minLength": 4}, "a\r\nb"),
        (
            {
                "type": "string",
                "contentMediaType": "application/json",
                "contentSchema": {"type": "integer", "minimum": 1},
            },
            0,
        ),
        (
            {
                "type": "string",
                "maxLength": 1,
                "contentMediaType": "application/json",
                "contentSchema": {"type": "integer"},
            },
            12,
        ),
    ],
)
def test_sse_validates_parsed_text_json_payload_and_wire_string(
    generate_api, checked, data_schema, value
):
    generated = generate_api(
        stream_operation("text/event-stream", event_schema(data_schema)),
        openapi_version="3.2.0",
        validate_responses=checked,
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")

    class Controller:
        async def watch(self, request):
            async def events():
                yield contracts.Watch.Event(data=value)

            return contracts.Watch.Ok(body=events())

    client = make_client(generated, Controller())
    if checked:
        with pytest.raises(msgspec.ValidationError):
            client.get("/events")
    else:
        assert client.get("/events").status_code == 200


@pytest.mark.parametrize("media_type", ["text/event-stream", "application/jsonl"])
def test_streams_preserve_nested_object_property_constraints(generate_api, media_type):
    item = {"$ref": "#/components/schemas/Item"}
    data = {"type": "string", "contentMediaType": "application/json", "contentSchema": item}
    generated = generate_api(
        stream_operation(
            media_type, event_schema(data) if media_type == "text/event-stream" else item
        ),
        {
            "Item": {
                "type": "object",
                "properties": {
                    "counts": {
                        "type": "object",
                        "minProperties": 2,
                        "additionalProperties": {"type": "integer"},
                    }
                },
            }
        },
        openapi_version="3.2.0",
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    models = importlib.import_module(generated.__name__ + ".models")

    class Controller:
        async def watch(self, request):
            async def events():
                item = models.Item(counts={"one": 1})
                yield (
                    contracts.Watch.Event(data=item) if media_type == "text/event-stream" else item
                )

            return contracts.Watch.Ok(body=events())

    with pytest.raises(msgspec.ValidationError, match="counts"):
        make_client(generated, Controller()).get("/events")


@pytest.mark.parametrize("outcome", ["complete", "error", "regular_response"])
def test_multipart_uploads_stay_open_until_the_stream_finishes(generate_api, outcome):
    generated = generate_api(
        {
            "/events": {
                "post": {
                    "operationId": "watch",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["file"],
                                    "properties": {"file": {"type": "string", "format": "binary"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "content": {"application/jsonl": {"itemSchema": {"type": "string"}}}
                        },
                        "204": {},
                    },
                }
            }
        },
        openapi_version="3.2.0",
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    uploads = []

    class Controller:
        async def watch(self, request):
            uploads.append(request.body.file)
            if outcome == "regular_response":
                return contracts.Watch.NoContent()

            async def events():
                yield (await request.body.file.read()).decode()
                if outcome == "error":
                    raise RuntimeError("stream failed")

            return contracts.Watch.Ok(body=events())

    client = make_client(generated, Controller())
    if outcome == "error":
        with pytest.raises(RuntimeError, match="stream failed"):
            client.post("/events", files={"file": ("hello.txt", b"hello")})
    else:
        response = client.post("/events", files={"file": ("hello.txt", b"hello")})
        assert response.content == (b'"hello"\n' if outcome == "complete" else b"")
    assert len(uploads) == 1 and uploads[0].file.closed


def test_disconnect_during_send_closes_the_paused_generator_with_async_cleanup(generate_api):
    generated = generate_api(
        stream_operation("application/jsonl", {"type": "integer"}), openapi_version="3.2.0"
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    produced = []
    closed = []

    class Controller:
        async def watch(self, request):
            async def events():
                try:
                    for value in range(10):
                        produced.append(value)
                        yield value
                finally:
                    await anyio.sleep(0)
                    closed.append(True)

            return contracts.Watch.Ok(body=events())

    app = Starlette(routes=generated.create_router(generated.Handlers(default=Controller())).routes)

    async def exercise():
        sending = anyio.Event()

        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sending.set()
                await anyio.sleep_forever()

        with anyio.fail_after(2):
            await app(asgi_scope(), receive, send)

    anyio.run(exercise)
    assert produced == [0]
    assert closed == [True]


@pytest.mark.parametrize("failure", ["source", "send"])
def test_transport_errors_do_not_hide_source_io_errors(generate_api, failure):
    generated = generate_api(
        stream_operation("application/jsonl", {"type": "integer"}), openapi_version="3.2.0"
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    closed = []

    class Controller:
        async def watch(self, request):
            async def events():
                try:
                    yield 1
                    raise OSError("source failed")
                finally:
                    closed.append(True)

            return contracts.Watch.Ok(body=events())

    app = Starlette(routes=generated.create_router(generated.Handlers(default=Controller())).routes)

    async def exercise():
        async def receive():
            await anyio.sleep_forever()
            raise AssertionError("receive unexpectedly resumed")

        async def send(message):
            if failure == "send" and message["type"] == "http.response.body":
                raise OSError("client disconnected")

        expected = OSError if failure == "source" else ClientDisconnect
        with pytest.raises(expected) as caught:
            await app(asgi_scope(), receive, send)
        if failure == "source":
            assert str(caught.value) == "source failed"

    anyio.run(exercise)
    assert closed == [True]
