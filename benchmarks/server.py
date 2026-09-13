"""Load just the selected generated package and its runtime dependencies."""

import importlib
import os

from starlette.applications import Starlette

package = os.environ["OAPI_BENCH_PACKAGE"]
generated = importlib.import_module(package)
c = importlib.import_module(package + ".contracts")
m = importlib.import_module(package + ".models")


class Controller:
    async def empty(self, request):
        return c.Empty.NoContent()

    async def parameters(self, request):
        return c.Parameters.NoContent()

    async def echo(self, request):
        return c.Echo.Ok(body=request.body)

    async def nested(self, request):
        return c.Nested.Ok(body=request.body)

    async def list_items(self, request):
        return c.ListItems.Ok(
            body=[
                m.Item(id=i, name=f"item {i}", quantity=i + 1, active=True)
                for i in range(100)
            ]
        )


router = generated.create_router(generated.Handlers(bench=Controller()))
app = Starlette(routes=router.routes)
