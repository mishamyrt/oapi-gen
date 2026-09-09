import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from dishka import FromDishka, Provider, Scope, make_async_container, provide
from dishka.integrations.starlette import StarletteProvider
from oapi_gen import generate_package
from oapi_gen_dishka import inject, setup_dishka
from starlette.applications import Starlette
from starlette.requests import Request


@pytest.mark.parametrize("streaming", [False, True])
def test_starlette_scopes_are_shared_isolated_and_closed(tmp_path: Path, monkeypatch, streaming):
    specification = {
        "openapi": "3.2.0" if streaming else "3.1.0",
        "info": {"title": "Scope test", "version": "1"},
        "paths": {
            "/value": {
                "get": {
                    "operationId": "test",
                    "security": [{"key": []}],
                    "responses": {
                        "200": {
                            "description": "value",
                            "content": {
                                "application/jsonl" if streaming else "application/json": {
                                    "itemSchema" if streaming else "schema": {"type": "string"}
                                }
                            },
                        },
                        "503": {"description": "Unavailable"},
                    },
                }
            }
        },
        "components": {
            "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}}
        },
    }
    source = tmp_path / "spec.json"
    source.write_text(json.dumps(specification))
    package_name = f"generated_starlette_dishka_{streaming}"
    generate_package(source, tmp_path / package_name)
    monkeypatch.syspath_prepend(str(tmp_path))
    generated = importlib.import_module(package_name)
    contracts = importlib.import_module(package_name + ".contracts")
    resources = []

    class Resource:
        def __init__(self, name):
            self.name = name
            self.closed = False

    class Services(Provider):
        @provide(scope=Scope.REQUEST)
        async def resource(self, request: Request) -> AsyncIterator[Resource]:
            resource = Resource(request.headers["X-Key"])
            resources.append(resource)
            try:
                yield resource
            finally:
                await asyncio.sleep(0)
                resource.closed = True

    class Security:
        @inject
        async def handle_key(
            self, context, operation_id, credential, resource: FromDishka[Resource]
        ):
            assert resource.name == credential.api_key
            return resource

    class Controller:
        @inject
        async def test(self, request, resource: FromDishka[Resource]):
            await asyncio.sleep(0)
            assert request.security_context is resource
            assert not resource.closed
            if resource.name == "fail":
                raise ValueError("failed")
            if resource.name == "unavailable":
                raise contracts.Test.ServiceUnavailable()
            if streaming:

                @inject
                async def check_resource(current: FromDishka[Resource]):
                    assert current is resource
                    assert not current.closed

                async def items():
                    await asyncio.sleep(0)
                    await check_resource()
                    yield resource.name
                    await check_resource()

                return contracts.Test.Ok(body=items())
            return contracts.Test.Ok(body=resource.name)

    async def scenario():
        container = make_async_container(Services(), StarletteProvider())
        router = generated.create_router(
            generated.Handlers(default=Controller()), security=Security()
        )
        app = Starlette(routes=router.routes)
        setup_dishka(container, app)
        try:
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                responses = await asyncio.gather(
                    *[client.get("/value", headers={"X-Key": str(i)}) for i in range(10)]
                )
                assert [response.json() for response in responses] == [str(i) for i in range(10)]
                transport.raise_app_exceptions = False
                assert (await client.get("/value", headers={"X-Key": "fail"})).status_code == 500
                assert (
                    await client.get("/value", headers={"X-Key": "unavailable"})
                ).status_code == 503
            assert len(resources) == 12
            assert all(resource.closed for resource in resources)
        finally:
            await container.close()

    asyncio.run(scenario())
