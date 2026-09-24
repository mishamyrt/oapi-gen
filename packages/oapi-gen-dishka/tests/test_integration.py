import asyncio
import importlib
import inspect
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from dishka import (
    FromComponent,
    FromDishka,
    Provider,
    Scope,
    make_async_container,
    provide,
)
from dishka.integrations.starlette import StarletteProvider
from dishka.integrations.starlette import inject as inject_starlette
from dishka.integrations.starlette import setup_dishka as setup_starlette_dishka
from oapi_gen import generate_package
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oapi_gen_dishka import inject, setup_dishka


@dataclass
class RequestResource:
    name: str
    id: str
    native_dependency_seen: bool = False
    closed: bool = False


class ResourceProvider(Provider):
    def __init__(self):
        super().__init__()
        self.resources: list[RequestResource] = []

    @provide(scope=Scope.REQUEST)
    async def resource(self, request: Request) -> AsyncIterator[RequestResource]:
        resource = RequestResource(name=request.headers.get("X-Request", "normal"), id=uuid4().hex)
        self.resources.append(resource)
        try:
            yield resource
        finally:
            resource.closed = True


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("generated")
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Dishka integration", "version": "1"},
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "x-handler-group": "Items",
                    "security": [{"bearerAuth": []}],
                    "parameters": [
                        {"name": "fail", "in": "query", "schema": {"type": "boolean"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Resource id",
                            "content": {"application/json": {"schema": {"type": "string"}}},
                        },
                    },
                },
            },
        },
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }
    source = root / "spec.json"
    source.write_text(json.dumps(spec))
    name = "generated_dishka_test_api"
    generate_package(source, root / name)
    sys.path.insert(0, str(root))
    try:
        yield importlib.import_module(name)
    finally:
        sys.path.remove(str(root))
        for key in list(sys.modules):
            if key == name or key.startswith(f"{name}."):
                del sys.modules[key]


@inject
async def outside_probe(*, resource: FromDishka[RequestResource]) -> RequestResource:
    return resource


def build_app(generated, started=None):
    contracts = generated.contracts
    provider = ResourceProvider()
    container = make_async_container(provider, StarletteProvider())

    class Controller:
        @inject
        async def list_items(
            self,
            request: contracts.ListItems.Request,
            *,
            resource: FromDishka[RequestResource],
        ) -> contracts.ListItems.Response:
            assert request.security_context is resource
            assert resource.native_dependency_seen
            assert not resource.closed
            if request.fail:
                raise ValueError("handler failed")
            if resource.name == "cancel":
                started.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            # Nested injection must still see this request's resource after yielding.
            assert await outside_probe() is resource
            return contracts.ListItems.Ok(body=f"{resource.name}:{resource.id}")

    class Security:
        @inject
        async def handle_bearer_auth(
            self,
            context: object | None,
            operation_id: str,
            credential: contracts.BearerAuthSecurity,
            *,
            resource: FromDishka[RequestResource],
        ) -> object:
            assert operation_id == "listItems"
            assert context is None
            if credential.token != "accepted":
                raise contracts.SecurityRejected("denied")
            return resource

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await container.close()

    router = generated.create_router(generated.Handlers(items=Controller()), security=Security())
    generated_endpoint = router.routes[0].endpoint

    @inject_starlette
    async def native_endpoint(request: Request, resource: FromDishka[RequestResource]):
        resource.native_dependency_seen = True
        return await generated_endpoint(request)

    app = Starlette(
        lifespan=lifespan,
        routes=[Route("/items", native_endpoint), *router.routes[1:]],
    )
    setup_dishka(container, app)
    return app, container, provider, Controller


def test_generated_handlers_share_native_scope_with_security_and_dependencies(
    generated,
):
    app, _, provider, controller = build_app(generated)
    with TestClient(app) as client:
        first = client.get("/items", headers={"Authorization": "Bearer accepted"})
        second = client.get("/items", headers={"Authorization": "Bearer accepted"})
        assert first.status_code == second.status_code == 200
        assert first.json() != second.json()
        assert len(provider.resources) == 2
        assert all(resource.closed for resource in provider.resources)
        operation = client.get("/openapi.json").json()["paths"]["/items"]["get"]
        assert [parameter["name"] for parameter in operation["parameters"]] == ["fail"]

    signature = inspect.signature(controller().list_items)
    assert list(signature.parameters) == ["request"]
    assert signature.parameters["request"].annotation is generated.contracts.ListItems.Request
    assert signature.return_annotation is generated.contracts.ListItems.Response


def test_parallel_http_requests_are_isolated(generated):
    async def scenario():
        app, container, provider, _ = build_app(generated)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                responses = await asyncio.gather(
                    *(
                        client.get(
                            "/items",
                            headers={
                                "Authorization": "Bearer accepted",
                                "X-Request": str(index),
                            },
                        )
                        for index in range(10)
                    )
                )
            assert all(response.status_code == 200 for response in responses)
            assert [response.json().split(":")[0] for response in responses] == list(
                map(str, range(10))
            )
            assert len({response.json().split(":")[1] for response in responses}) == 10
            assert len(provider.resources) == 10
            assert all(resource.closed for resource in provider.resources)
            with pytest.raises(RuntimeError, match="inside an HTTP request"):
                await outside_probe()
        finally:
            await container.close()

    asyncio.run(scenario())


def test_failures_finalize_dependencies_and_reset_context(generated):
    async def scenario():
        app, container, provider, _ = build_app(generated)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with pytest.raises(ValueError, match="handler failed"):
                    await client.get(
                        "/items?fail=true", headers={"Authorization": "Bearer accepted"}
                    )
                assert provider.resources[-1].closed
                with pytest.raises(RuntimeError, match="inside an HTTP request"):
                    await outside_probe()
                denied = await client.get("/items", headers={"Authorization": "Bearer denied"})
                assert denied.status_code == 401
                assert provider.resources[-1].closed
                success = await client.get("/items", headers={"Authorization": "Bearer accepted"})
                assert success.status_code == 200
                assert all(resource.closed for resource in provider.resources)
                with pytest.raises(RuntimeError, match="inside an HTTP request"):
                    await outside_probe()
        finally:
            await container.close()

    asyncio.run(scenario())


def test_cancellation_finalizes_request_resources(generated):
    async def scenario():
        started = asyncio.Event()
        app, container, provider, _ = build_app(generated, started)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                task = asyncio.create_task(
                    client.get(
                        "/items",
                        headers={
                            "Authorization": "Bearer accepted",
                            "X-Request": "cancel",
                        },
                    )
                )
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert len(provider.resources) == 1
                assert provider.resources[0].closed
                with pytest.raises(RuntimeError, match="inside an HTTP request"):
                    await outside_probe()
        finally:
            await container.close()

    asyncio.run(scenario())


def test_dishka_components_are_supported():
    class ComponentProvider(Provider):
        @provide(scope=Scope.REQUEST)
        def resource(self) -> RequestResource:
            return RequestResource(name="component", id="component-id")

    @inject
    async def handler(
        *,
        resource: Annotated[RequestResource, FromComponent("other")],
    ) -> str:
        return resource.name

    container = make_async_container(ComponentProvider().to_component("other"))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await container.close()

    async def endpoint(request: Request):
        return JSONResponse(await handler())

    app = Starlette(lifespan=lifespan, routes=[Route("/", endpoint)])

    setup_dishka(container, app)
    with TestClient(app) as client:
        assert client.get("/").json() == "component"


@pytest.mark.parametrize("native_first", [False, True])
def test_setup_rejects_duplicate_scope_management(native_first):
    async def scenario():
        container = make_async_container(Provider())
        app = Starlette()
        try:
            (setup_starlette_dishka if native_first else setup_dishka)(container, app)
            middleware_count = len(app.user_middleware)
            with pytest.raises(RuntimeError, match="already configured"):
                setup_dishka(container, app)
            assert len(app.user_middleware) == middleware_count
        finally:
            await container.close()

    asyncio.run(scenario())


def test_inject_rejects_sync_functions():
    with pytest.raises(TypeError, match="async function or method"):
        inject(lambda: None)


def test_decorated_handlers_implement_generated_protocol(generated, tmp_path):
    code = """
from dishka import FromDishka
from oapi_gen_dishka import inject
from generated_dishka_test_api import contracts

class Controller:
    @inject
    async def list_items(
        self,
        request: contracts.ListItems.Request,
        *,
        dependency: FromDishka[object],
    ) -> contracts.ListItems.Response:
        return contracts.ListItems.Ok(body=str(request.fail))

handler: contracts.ItemsHandler = Controller()
handlers = contracts.Handlers(items=Controller())

async def call() -> contracts.ListItems.Response:
    return await handler.list_items(
        request=contracts.ListItems.Request(fail=None, security_context=None),
    )
"""
    source = tmp_path / "example.py"
    source.write_text(code)
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": ["example.py"],
                "pythonVersion": "3.12",
                "typeCheckingMode": "standard",
                "extraPaths": [
                    str(Path(generated.__file__).parents[1]),
                    str(Path(__file__).resolve().parents[1] / "src"),
                ],
            }
        )
    )
    command = [
        sys.executable,
        "-m",
        "basedpyright",
        "--project",
        str(config),
        "--pythonpath",
        sys.executable,
        "--outputjson",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

    source.write_text(
        code + '\nasync def invalid() -> None:\n    await handler.list_items("wrong")\n'
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    assert [diagnostic["rule"] for diagnostic in diagnostics] == ["reportArgumentType"]


def test_readme_example_runs_with_complete_generated_api(tmp_path, monkeypatch):
    integration_root = Path(__file__).resolve().parents[1]
    repo_root = integration_root.parents[1]
    example = (
        integration_root.joinpath("README.md")
        .read_text()
        .split("```python\n", 1)[1]
        .split("```", 1)[0]
    )
    generate_package(
        repo_root / "packages/oapi-gen/tests/fixtures/cats.openapi.yaml",
        tmp_path / "app/http/generated",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        namespace = {}
        exec(compile(example, "README.md", "exec"), namespace)
        with TestClient(namespace["app"]) as client:
            assert client.get("/api/cats").json() == []
            assert client.post("/api/cats", json={"name": "Mittens"}).status_code == 201
            cats = client.get("/api/cats?limit=1").json()
            assert len(cats) == 1
            assert cats[0]["name"] == "Mittens"
            assert client.get(f"/api/cats/{cats[0]['id']}").json() == cats[0]
            assert client.get(f"/api/cats/{uuid4()}").status_code == 404
            assert client.get("/api/cats?limit=0").status_code == 422
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
