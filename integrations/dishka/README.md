# oapi-gen-dishka

Inject Dishka dependencies into the async methods called by an `oapi-gen` router.
Generated code stays unchanged. The integration reuses Dishka's native Starlette
request container, so operation handlers, security handlers, and ordinary Starlette
endpoints share the same `Scope.REQUEST` instances.

Use the same version of `oapi-gen-dishka` as the `oapi-gen` generator that produced
your application code. Both packages are published together; see
[the release instructions](../../README.md#releases).

```bash
uv add oapi-gen-dishka
```

This directory is also independently buildable. To install from a local checkout:

```bash
uv add /path/to/oapi-gen/integrations/dishka
```

## Usage

The example uses the repository's complete Cats specification. Generate it into
your application's package:

```bash
oapi-gen generate tests/fixtures/cats.openapi.yaml --output app/http/generated
```

```python
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from dishka import FromDishka, Provider, Scope, make_async_container, provide
from starlette.applications import Starlette
from oapi_gen_dishka import inject, setup_dishka

from app.http.generated import Handlers, contracts, create_router, models


class CatsRepository:
    def __init__(self):
        self.cats: dict[UUID, models.Cat] = {}


class CatsController:
    @inject
    async def list_cats(
        self,
        request: contracts.ListCats.Request,
        *,
        repository: FromDishka[CatsRepository],
    ) -> contracts.ListCats.Response:
        cats = list(repository.cats.values())
        return contracts.ListCats.Ok(body=cats[: request.limit])

    @inject
    async def create_cat(
        self,
        request: contracts.CreateCat.Request,
        *,
        repository: FromDishka[CatsRepository],
    ) -> contracts.CreateCat.Response:
        cat = models.Cat(id=uuid4(), name=request.body.name)
        repository.cats[cat.id] = cat
        return contracts.CreateCat.Created()

    @inject
    async def show_cat_by_id(
        self,
        request: contracts.ShowCatById.Request,
        *,
        repository: FromDishka[CatsRepository],
    ) -> contracts.ShowCatById.Response:
        cat = repository.cats.get(request.cat_id)
        if cat is None:
            raise contracts.ShowCatById.NotFound(
                body=models.HttpError(message="Cat not found"),
            )
        return contracts.ShowCatById.Ok(body=cat)


class AppProvider(Provider):
    # This in-memory example keeps cats between requests in one process.
    repository = provide(CatsRepository, scope=Scope.APP)


container = make_async_container(AppProvider())


@asynccontextmanager
async def lifespan(app: Starlette):
    try:
        yield
    finally:
        await container.close()


router = create_router(Handlers(cats=CatsController()), prefix="/api")
app = Starlette(lifespan=lifespan, routes=router.routes)
setup_dishka(container, app)
```

Use `Scope.REQUEST` for services or database sessions that must be recreated for
each request. Dishka resolves dependencies from the normal provider graph and
finalizes generator providers at the end of the request, including on exceptions.
Add Dishka's `StarletteProvider()` from `dishka.integrations.starlette` when a
provider needs `starlette.requests.Request`.

`@inject` also works on generated security handler methods. Use
`dishka.integrations.starlette.inject` on ordinary Starlette endpoints; both
decorators resolve from the same container after the single setup call above.

## Boundaries

- Async HTTP handlers only. WebSockets, sync containers, and background work
  outside the request lifetime are not supported by this decorator.
- Call this package's `setup_dishka` once, before startup, **instead of** calling
  `dishka.integrations.starlette.setup_dishka` separately.
- The controller instance passed to `Handlers` is shared. Keep request-specific
  state in local variables and injected dependencies, not on `self`.
- Providers must register dependencies; `FromDishka[T]` selects the registered
  type and does not register it automatically. Dishka components are supported.
- Runtime signatures omit injected parameters and retain the remaining
  annotations. The decorator preserves the static response type but uses
  `Callable[..., ...]` for arguments, since Python typing cannot remove arbitrary
  `FromDishka` parameters. Annotate controller references with the generated
  protocol when calling them directly to check request argument types:

  ```python
  controller: contracts.CatsHandler = CatsController()
  ```

## Development

```bash
uv sync --project integrations/dishka
uv run --project integrations/dishka pytest integrations/dishka/tests
uv run --project integrations/dishka ruff check integrations/dishka
uv run --project integrations/dishka basedpyright --project integrations/dishka
uv build integrations/dishka --out-dir integrations/dishka/dist
```

The local development dependency on `oapi-gen` is used only for integration tests;
the published wheel does not depend on the generator at runtime.
