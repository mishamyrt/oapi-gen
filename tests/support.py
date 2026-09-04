"""Shared test types and helpers for OpenAPI content and generated clients."""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.testclient import TestClient

type SpecWriter = Callable[[str, str], Path]
type PackageImporter = Callable[[Path], ModuleType]


class ApiGenerator(Protocol):
    def __call__(
        self,
        paths: dict[str, Any],
        schemas: dict[str, Any] | None = None,
        *,
        validate_responses: bool = True,
        security_schemes: dict[str, Any] | None = None,
    ) -> ModuleType: ...


def request_body(schema: dict[str, Any]) -> dict[str, Any]:
    return {"required": True, "content": {"application/json": {"schema": schema}}}


def json_response(schema: dict[str, Any]) -> dict[str, Any]:
    return {"description": "Value", "content": {"application/json": {"schema": schema}}}


def make_client(generated: ModuleType, controller: object) -> TestClient:
    router = generated.create_router(generated.Handlers(default=controller))
    app = Starlette(routes=router.routes)
    return TestClient(app)
