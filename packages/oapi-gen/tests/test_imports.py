from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from oapi_gen import generate_package
from starlette.applications import Starlette
from starlette.testclient import TestClient

from .support import (
    ApiGenerator,
    PackageImporter,
    SpecWriter,
)


def test_model_names_do_not_shadow_generated_contracts(
    tmp_path: Path, write_specification: SpecWriter, import_generated: PackageImporter
) -> None:
    specification = write_specification(
        "model-name-collision.yaml",
        """
openapi: 3.1.0
info: {title: Model collision, version: 1.0.0}
paths:
  /value:
    get:
      operationId: foo
      responses:
        '200':
          description: Value
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Foo'}
components:
  schemas:
    Foo:
      type: object
      required: [value]
      properties:
        value: {type: string}
""",
    )
    output = tmp_path / "generated_model_collision"
    generate_package(specification, output)
    generated = import_generated(output)
    contracts = importlib.import_module("generated_model_collision.contracts")
    models = importlib.import_module("generated_model_collision.models")

    class Controller:
        async def foo(self, request):
            return contracts.Foo.Ok(body=models.Foo(value="ok"))

    router = generated.create_router(generated.Handlers(default=Controller()))
    app = Starlette(routes=router.routes)

    response = TestClient(app).get("/value")

    assert response.status_code == 200
    assert response.json() == {"value": "ok"}


@pytest.mark.parametrize("streaming", [False, True])
def test_contracts_import_without_starlette_and_router_loads_lazily(
    tmp_path: Path, generate_api: ApiGenerator, streaming: bool
) -> None:
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "responses": {
                        **(
                            {
                                "200": {
                                    "content": {
                                        "text/event-stream": {
                                            "itemSchema": {
                                                "type": "object",
                                                "required": ["data"],
                                                "properties": {"data": {"type": "string"}},
                                            }
                                        }
                                    }
                                },
                            }
                            if streaming
                            else {}
                        ),
                        "204": {
                            "description": "Cookie",
                            "headers": {
                                "Set-Cookie": {
                                    "schema": {"type": "array", "items": {"type": "string"}}
                                }
                            },
                        },
                    },
                }
            }
        },
        {"Value": {"type": "string"}},
        openapi_version="3.2.0" if streaming else "3.1.0",
    )
    script = """
import importlib
import importlib.abc
import sys

sys.path.insert(0, sys.argv[1])
name = sys.argv[2]
class NoStarlette(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'starlette' or fullname.startswith('starlette.'):
            raise ModuleNotFoundError('Starlette is unavailable')

blocker = NoStarlette()
sys.meta_path.insert(0, blocker)
package = importlib.import_module(name)
contracts = importlib.import_module(name + '.contracts')
cookie = contracts.Cookie(name='theme', value='dark')
assert cookie.to_header() == 'theme=dark; Path=/; SameSite=lax'
importlib.import_module(name + '.models')
assert name + '.router' not in sys.modules
assert 'starlette' not in sys.modules
try:
    package.missing
except AttributeError:
    pass
else:
    raise AssertionError('unknown attributes must fail')
sys.meta_path.remove(blocker)
factory = package.create_router
from starlette.routing import Router
assert isinstance(factory(package.Handlers(default=object())), Router)
assert package.create_router is factory
assert factory.__module__ == name + '.router'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), generated.__name__],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
