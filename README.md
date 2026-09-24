<h1 align="center">
    <img width="100" src="./assets/logo.svg" alt="oapi-gen logo" /><br>
        oapi-gen <br/>
</h1>
<p align="center">
    Router generator for Starlette.<br/><br/>
</p>

`oapi-gen` turns an OpenAPI document into typed Python models, handler interfaces,
and a Starlette router. You write the handler methods; the generated router reads
and validates requests, calls your handlers, and serializes their responses.

Requires Python 3.12+. Generated applications use Starlette and msgspec at runtime
and do not depend on the generator or Pydantic.

## Quick start

### 1. Install

Run these commands in your application's uv project:

```bash
uv add --dev oapi-gen
uv add starlette msgspec 'uvicorn[standard]'
```

For APIs with file uploads or other `multipart/form-data` bodies, also run
`uv add python-multipart`.

### 2. Describe the API and generate code

Save this as `openapi.yaml`. It defines a `GET /cats` endpoint with an optional
`limit` query parameter:

```yaml
openapi: 3.1.0
info: {title: Cats API, version: 1.0.0}
paths:
  /cats:
    get:
      operationId: listCats
      tags: [Cats]
      summary: List cats
      parameters:
        - name: limit
          in: query
          description: Maximum number of cats
          schema: {type: integer, minimum: 1, maximum: 100, default: 20}
      responses:
        '200':
          description: Cat list
          content:
            application/json:
              schema:
                type: array
                items: {$ref: '#/components/schemas/Cat'}
components:
  schemas:
    Cat:
      type: object
      required: [id, name]
      properties:
        id: {type: integer}
        name: {type: string}
```

Generate a Python package from the spec:

```bash
uv run oapi-gen generate openapi.yaml --output app/http/generated
```

The command creates the output directory and its parents.

### 3. Implement the handler

Save this as `app/main.py`:

```python
from starlette.applications import Starlette

from app.http.generated import Handlers, create_router, models
from app.http.generated.contracts import ListCats


class CatsController:
    async def list_cats(self, request: ListCats.Request) -> ListCats.Response:
        cats = [models.Cat(id=1, name="Mittens"), models.Cat(id=2, name="Luna")]
        return ListCats.Ok(body=cats[: request.limit])


handlers = Handlers(cats=CatsController())
router = create_router(handlers, prefix="/api")
app = Starlette(routes=router.routes)
```

The spec's `operationId: listCats` becomes the `list_cats` method, and `tags: [Cats]`
places it in the `cats` handler group. The controller does not need a base class;
its method names and types must match the generated interface.

### 4. Run the server

From your application directory:

```bash
uv run uvicorn app.main:app
```

In another terminal:

```bash
curl 'http://127.0.0.1:8000/api/cats?limit=1'
# [{"id":1,"name":"Mittens"}]
```

The OpenAPI document is served at `/api/openapi.json`. Invalid requests, such as
`limit=0`, receive HTTP 422 before the handler runs.

## Working with generated code

The example above generates:

| Import | Purpose |
| --- | --- |
| `models.Cat` | A typed data model from `components.schemas.Cat`. |
| `ListCats.Request` | The parsed request, including `limit`. |
| `ListCats.Ok` | The declared HTTP 200 response. |
| `ListCats.Response` | The allowed return type for the handler. |
| `Handlers` | The container where you bind your controller instances. |
| `create_router` | The function that builds the Starlette router. |

Each operation gets its own request and response types. Return declared responses
below 400; raise declared 4xx/5xx variants as exceptions. See
[responses and errors](docs/usage.md#responses-and-errors).

Keep your handlers outside the generated directory. After changing the spec,
rerun `generate` and update your handlers to match. The generator overwrites the
files it manages and removes obsolete generated files.

Commit the generated directory and check it in CI:

```bash
uv run oapi-gen check openapi.yaml --output app/http/generated
```

`check` exits with status 1 if generated files are missing or stale. Use the same
options for `generate` and `check`.

## Supported OpenAPI features

The generator supports OpenAPI 3.0.x, 3.1.x, and part of 3.2.x, including:

- Typed path, query, header, and cookie parameters.
- JSON and multipart request bodies; JSON and binary file responses.
- Typed response headers and multiple cookies.
- API keys, HTTP basic/bearer, and OAuth2 security.
- SSE, JSON Lines, NDJSON, and JSON Sequence response streams in OpenAPI 3.2.

Every operation needs a unique `operationId`. Define JSON objects in
`components.schemas` and reference them with `$ref`. Responses need fixed numeric
status codes; `default` and wildcard codes are not supported.

Unsupported constructs fail generation with an error. See the
[full scope and schema rules](docs/usage.md#supported-openapi-features) before adapting
an existing specification.

## More examples and options

| Task | Guide |
| --- | --- |
| Organize handler groups or package generated code | [Generated packages](docs/usage.md#generated-packages) |
| Return errors, headers, or cookies | [Responses and errors](docs/usage.md#responses-and-errors) |
| Authenticate requests | [Security](docs/usage.md#security) |
| Inject dependencies with Dishka | [oapi-gen-dishka](packages/oapi-gen-dishka/README.md) |
| Return files or stream events | [Files](docs/usage.md#file-responses) · [Streaming](docs/usage.md#streaming-responses-openapi-32) |
| Configure response validation | [Validation](docs/usage.md#response-validation) · [Benchmarks](benchmarks/README.md) |
| Upgrade previously generated code | [Migration notes](docs/usage.md#upgrading-generated-code) |

Use the same version of `oapi-gen-dishka` as the generator that produced your code.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for workspace setup, tests, code structure,
and releases.
