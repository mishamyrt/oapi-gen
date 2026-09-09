# oapi-gen

`oapi-gen` generates implementation-facing Python 3.12+ contracts and HTTP adapters
from an OpenAPI document using Starlette + msgspec. Handler
protocols and request/response envelopes remain independent of the HTTP framework
and dependency injection. Handler implementations are bound explicitly when the
router is created.

## Installation

```bash
uv add --dev oapi-gen
uv add starlette msgspec 'uvicorn[standard]'
```

The generator checks msgspec codec compatibility before writing output files.
Generated applications need Starlette and msgspec at runtime; they do not import
the generator or Pydantic. APIs that use `multipart/form-data` must also install
`python-multipart`.

## Usage

Save this minimal API as `openapi.yaml` in your application directory:

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

Generate the package; the command creates its parent directories:

```bash
oapi-gen generate openapi.yaml --output app/http/generated
```

Save the following as `app/main.py`. Implement the generated protocol without
inheriting from it, then bind each handler group explicitly:

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

Start the server from the same application directory:

```bash
uv run uvicorn app.main:app
```

In another terminal:

```bash
curl 'http://127.0.0.1:8000/api/cats?limit=1'
# [{"id":1,"name":"Mittens"}]
curl 'http://127.0.0.1:8000/api/openapi.json'
```

For CI, commit the generated directory and run:

```bash
oapi-gen check openapi.yaml --output app/http/generated
```

`check` exits with status 1 when the generated files are missing or stale.
Operation and field descriptions are included in generated contract docstrings,
so they remain available while implementing handlers in an IDE.

Handler protocols use the group name with a `Handler` suffix, for example
`AuthHandler` and `CatsHandler`. Operations without a group use `DefaultHandler`.
The `security` group uses `SecurityHandler_2` because `SecurityHandler` is reserved
for authorization. Regenerate existing packages and update imports from `*Api`
to `*Handler`.

`API_INFO_TITLE` and `API_INFO_VERSION` contain the corresponding values from the
OpenAPI `info` object.

For Dishka, the separate [`oapi-gen-dishka`](integrations/dishka/README.md) package
adds `@inject` and `FromDishka[T]` injection to handler methods using the native
Starlette request scope.

Each operation has a namespace in `contracts`: `Login.Request` is its request
envelope, `Login.Ok` is a response variant, and `Login.Unauthorized` is an exception.
Return responses below 400 and raise declared 4xx/5xx errors:

```python
async def login(self, request: contracts.Login.Request) -> contracts.Login.Response:
    if not accepted:
        raise contracts.Login.Unauthorized(body="Invalid credentials")
    return contracts.Login.Ok(body=session)
```

`Login.Response` includes only the returnable variants (`Ok` in this example).
For an operation with only error statuses, `Response` is `typing.Never`.
Status names follow HTTP names such as
`Ok` (200), `Created` (201), `NoContent` (204), `NotFound` (404), and
`UnprocessableEntity` (422). Custom statuses use names such as `Status499`.
The router catches only error variants declared for that operation, including when
raised by a nested service call. Other exceptions propagate normally. Error bodies
and headers use the same serialization and validation as returned responses.
Returning an error instead of raising it, or returning an undeclared variant such
as another operation's `Ok`, raises `TypeError`. Declared response headers remain
typed fields on each variant:

```python
return contracts.CreateUpload.Created(
    body=upload,
    x_request_id=request_id,
)
```

Header fields that collide with `body` or exception attributes receive a `_header`
suffix, for example the `Args` header on an error becomes `args_header`.

To set multiple cookies, declare `Set-Cookie` as an array of strings in the
response headers. OpenAPI describes the serialized header values; the Python
contract accepts `list[contracts.Cookie]`:

```yaml
responses:
  '200':
    description: Logged in
    headers:
      Set-Cookie:
        schema:
          type: array
          items: {type: string}
```

Pass a `Cookie` object for each cookie:

```python
return contracts.Login.Ok(
    set_cookie=[
        contracts.Cookie(name="session", value="abc", httponly=True, secure=True),
        contracts.Cookie(
            name="refresh",
            value="xyz",
            path="/auth",
            max_age=604800,
            httponly=True,
            secure=True,
        ),
    ],
)
```

Cookie options are `name`, `value` (default `""`), `path` (default `"/"`),
`domain`, `max_age` in seconds, `expires` as a UTC-aware `datetime`, `secure`,
`httponly`, and `samesite` (`"lax"`, `"strict"`, `"none"`, or `None`; default `"lax"`).
Use `max_age=0` to delete a cookie, matching its original name, path, and domain.

The router escapes values and emits a separate `Set-Cookie` header for each object.
An empty list or an omitted optional value emits no cookie headers. This also works
for raised error variants. Response validation checks both cookie fields and the
serialized values against the OpenAPI header schema.
A `Set-Cookie` declared as a string still sets one cookie; arrays in other response
headers keep their comma-separated serialization. Regenerate the package after
changing the schema or upgrading the generator. Replace any raw strings in
`set_cookie=[...]` with `contracts.Cookie(...)` objects.

When migrating generated handlers, replace `LoginRequest` with `Login.Request`,
`LoginResponse` with `Login.Response`, and `LoginResponse200` with `Login.Ok`.
Replace `return Login.Unauthorized(...)` (and other 4xx/5xx variants) with
`raise Login.Unauthorized(...)`.
Regenerate the package and update all callers together.
Operation names that collide with contract infrastructure receive an `Operation`
suffix, for example `handlers` becomes `HandlersOperation` because `Handlers`
is the handler container. If that name is also occupied, a numeric suffix is added.

When an API declares security requirements, implement the generated
`SecurityHandler` protocol and bind it separately from the operation handlers. The
generated router extracts credentials, passes the operation ID and declared
scopes/roles to the security handler, and enforces OpenAPI's OR/AND semantics.
Each security method receives the previous context and returns the context exposed
to the operation handler as `request.security_context`. Schemes combined in one
requirement are evaluated in declaration order and share that context. Alternatives
are evaluated independently; raise the generated `SecurityRejected` exception to
reject one alternative and allow the router to try the next one. Other exceptions,
including `starlette.exceptions.HTTPException`, abort authorization immediately.
Missing or malformed credentials reject only their own alternative. Authorization
runs before parameters and request bodies are read or validated; denied requests
therefore do not parse JSON or spool uploaded files.

```python
router = create_router(handlers, security=security_handler)
app = Starlette(routes=router.routes)
```

## Current scope

Version 0.1 intentionally supports a strict subset:

- OpenAPI 3.0.x, 3.1.x and the supported subset of 3.2.x;
- internal references to schemas, response headers, and security schemes in `components`;
- scalar path, query, header, and cookie parameters with their default serialization;
- arrays of scalars in query parameters (repeated values) and path/header parameters
  (comma-separated values, including repeated header lines);
- API key (header, query, or cookie), HTTP basic/bearer, and OAuth2 security;
- OAuth2 Device Authorization, `oauth2MetadataUrl`, and security scheme `deprecated`;
- security requirement alternatives (OR), combined schemes (AND), and operation overrides;
- one JSON or multipart request media type and one response media type per status;
- JSON responses and `itemSchema` response streams using SSE, JSON Lines, NDJSON or JSON Sequence;
- internal `components.mediaTypes` references in request and response content;
- response `summary` docstrings and optional response `description` in OpenAPI 3.2;
- multipart object bodies with scalar form fields and binary file uploads;
- typed response headers with default simple serialization, plus separate
  `Set-Cookie` headers for arrays of cookie strings;
- fixed numeric response status codes;
- grouping by `x-handler-group`, falling back to the first tag.

External references, OpenID Connect/mTLS, callbacks, webhooks, custom parameter or
multipart serialization, streaming requests, multipart responses, and wildcard/default
response codes fail generation with an actionable error. They are not silently ignored.

Object parameters, request cookie arrays, nested arrays, and composed array
parameters are also rejected during generation. JSON schema constraints for numeric bounds,
`multipleOf`, string length and patterns, and array length are preserved in requests
and validated responses, including nested values and referenced scalar/array schemas.
Unsupported constraints such as `uniqueItems`, `contains`, conditional schemas, and
`propertyNames` produce a generation error, including on multipart roots.
The HTTP adapter enforces `minProperties` and `maxProperties` on JSON objects,
including nested models, dictionaries, and references, and on multipart roots.
Requests count supplied keys (including extra keys); validated responses count
serialized keys (including model defaults). Multipart counts unique field names,
so repeated parts of an array count as one property. Bounds must be non-negative integers.
File arrays enforce `minItems` and `maxItems`; other file constraints fail generation.

Common authoring rules and supported alternatives:

| Construct | Rule / alternative |
| --- | --- |
| `operationId` | Required for every operation; must remain unique after Python name normalization. |
| Inline object with `properties` | Move JSON objects to `components.schemas` and use `$ref`. Multipart bodies may declare fields inline. |
| Inline `allOf` | Only a single member is supported; move multi-member object composition to components. |
| `oneOf` | Branches must have disjoint explicit JSON types, or object branches must declare a discriminator with required, disjoint string `const`/`enum` values. Overlapping or unproven alternatives fail generation. Use `anyOf` when overlap is intended. |
| `readOnly: true` / `writeOnly: true` | Rejected. Define separate input/output components such as `CreateUser` and `UserResponse`; put passwords only in the input model and server IDs only in the output model. |
| `$ref` with bounds | Bounds accumulate across aliases; a sibling bound cannot weaken the referenced schema. Nested collection values preserve the same constraints. |
| Path parameter names | Wire names such as `item-id` are mapped to Python names for routing. Validation errors and served OpenAPI retain the original names. |

Both `oneOf` and `anyOf` must also satisfy msgspec codec restrictions; in particular,
multiple object variants require a supported discriminator.

Responses serialize model fields using their OpenAPI names, including aliases in
nested models. Importing generated models or contracts does not load Starlette; the
existing `create_router` export loads the HTTP adapter when first accessed.

## Runtime

The adapter explicitly reads query/path/header/cookie parameters, decodes JSON
bytes directly into `msgspec.Struct` models, and encodes responses directly to
bytes. Codecs are created once. Multipart uploads close when the handler finishes,
including after validation failures or exceptions. Authorization follows the declared
security alternatives and scopes.

The original OpenAPI document is emitted as `openapi.json` and served at
`<prefix>/openapi.json`; route prefixes are applied when the router is created.
Include this JSON file as package data when distributing the generated package.
Pass `include_schema=False` to omit the schema route.

### Streaming responses (OpenAPI 3.2)

Declare `itemSchema` for each independently validated stream item. Generated response
bodies accept `AsyncIterable[T]`; the adapter sends one item at a time and awaits the
HTTP send before requesting another item.

| Media type | Contract item | Wire format |
| --- | --- | --- |
| `text/event-stream` | Generated operation event dataclass | SSE fields and a blank line |
| `application/jsonl` | Schema type or model | JSON followed by LF |
| `application/x-ndjson` | Schema type or model | JSON followed by LF |
| `application/json-seq` | Schema type or model | RS (`0x1E`), JSON, LF |

The [complete streaming example](tests/fixtures/streaming.openapi.yaml) includes all
four formats, reusable media types, a typed SSE payload, and Device Authorization.
For example, a response can reuse a Media Type Object:

```yaml
# In an OpenAPI 3.2 document; Cat is an existing schema component.
paths:
  /cats/events:
    get:
      operationId: watchCats
      tags: [Cats]
      responses:
        '200':
          summary: Cat changes
          content:
            text/event-stream:
              $ref: '#/components/mediaTypes/CatEvents'
components:
  mediaTypes:
    CatEvents:
      itemSchema:
        $ref: '#/components/schemas/CatEvent'
  schemas:
    CatEvent:
      type: object
      required: [event, data]
      properties:
        event: {type: string, const: cat.updated}
        id: {type: string}
        data:
          type: string
          contentMediaType: application/json
          contentSchema:
            $ref: '#/components/schemas/Cat'
```

SSE `data` is a string on the wire. With a JSON `contentMediaType`, its Python
contract uses the `contentSchema` type and the adapter JSON-encodes it. Without
`contentSchema`, JSON data has type `Any`; without `contentMediaType`, it remains
text. The original schema and its generated wire model keep `data: str`.

```python
from collections.abc import AsyncIterator
from app.http.generated import models
from app.http.generated.contracts import WatchCats


class CatsController:
    async def watch_cats(self, request: WatchCats.Request) -> WatchCats.Response:
        async def events() -> AsyncIterator[WatchCats.Event]:
            yield WatchCats.Event(
                event="cat.updated",
                id="1",
                data=models.Cat(id=1, name="Mittens"),
            )

        return WatchCats.Ok(body=events())
```

The 200 SSE response uses `Operation.Event`; other statuses use names such as
`Operation.CreatedEvent`. Events are frozen dataclasses. Only fields declared in
the item schema appear in the contract, and optional fields default to `None`
(omitted on the wire). A JSON Lines/NDJSON/JSON Sequence handler instead yields
the item models or scalar values directly.

SSE item schemas currently require a flat object with a required `data` string
and may also declare `event`, `id` and `retry` properties. `event` and `id` are strings;
`retry` is a non-negative integer in milliseconds. Event unions/composition,
additional event fields and `contentEncoding` fail generation. Referenced schemas
are supported; inline payload objects follow the existing rule requiring a named
schema component. Streaming responses require `itemSchema`; whole-stream `schema`
constraints, streaming request bodies, HEAD streams and bodyless status streams
are rejected. Declare one media type per response status.

Response validation runs before each item is sent, including nested constraints,
SSE JSON payload constraints and constraints on the serialized `data` string.
`--no-validate-responses` skips schema validation but still checks the response
variant, SSE event class and mandatory SSE framing rules. Multiline text uses one
`data:` line per line; CR/CRLF are normalized to LF before validation. Invalid
newlines in `event`/`id`, NUL in `id`, and invalid `retry` values are rejected.

SSE responses default to `Cache-Control: no-cache` and send a comment heartbeat
every 15 seconds. `Content-Length` cannot be declared for a stream. Declare
`Last-Event-ID` as an ordinary header parameter when the application supports
resuming a subscription; storing events and replaying them is application logic.
Configure reverse proxies to forward streaming chunks without buffering.

Authorization and request validation finish before the handler runs. Raise declared
HTTP errors before returning the streaming response. After headers are sent, an
iterator or validation error terminates the stream and propagates; it cannot
replace the HTTP status or emit an undeclared error event.

The adapter cancels iteration on client disconnect and calls the source iterator's
`aclose()` when available. Multipart uploads stay open until the stream finishes,
and Dishka request dependencies remain available during iteration. Acquire
subscription resources inside the iterator and release them in `finally` or an
async context manager. Cleanup that awaits while handling cancellation should use
AnyIO's `CancelScope(shield=True)`, as with other cancellable Starlette code.

### OpenAPI 3.2 metadata and security

Response `summary` and `description` are combined in generated response docstrings.
Both may be omitted in 3.2; 3.0/3.1 still require `description`. Media Type Objects
can be reused through internal `$ref` chains under `components.mediaTypes` for
both requests and responses. Missing, cyclic and external references fail generation.

OAuth2 `flows.deviceAuthorization` requires `deviceAuthorizationUrl`, `tokenUrl`
and `scopes`. The resource-server contract still receives a bearer token and the
operation's declared scopes through `SecurityHandler`; the generator does not
implement the authorization server's device flow. `oauth2MetadataUrl` must be an
HTTPS URL. It and `deprecated` are retained in the served schema and documented on
the generated credential class. Deprecation does not disable authentication.

OpenAPI 3.2 support remains selective. In particular, `query` and
`additionalOperations` produce explicit errors rather than silently dropping routes.

### Response validation and maximum throughput

Responses are validated by default. For trusted handler implementations,
generate a faster adapter with:

```bash
oapi-gen generate spec/openapi.yaml --output app/http/generated \
  --no-validate-responses
oapi-gen check spec/openapi.yaml --output app/http/generated \
  --no-validate-responses
```

This keeps input validation and the check for declared response variants. It
omits runtime validation of response bodies and headers. Static checking remains
available through generated protocols, dataclasses and Struct models, but cannot
prove numeric bounds, lengths, patterns, or the validity of values obtained from
untyped code. Struct constructors themselves do not validate field values.

In checked mode, msgspec responses are converted to builtins and validated before
encoding. This also checks existing Struct instances, applies defaults and filters
undeclared fields where the schema ignores them. In trusted mode, encoding operates
directly on the handler result.

### Validation semantics

- JSON decoding is strict: a string such as `"2"` does not satisfy an integer field.
  Textual HTTP parameters and form values use explicit coercion.
- Model field names use `msgspec.field(name=...)` aliases; pass Python field names
  when constructing models. Non-object root schemas become type aliases.
- Missing optional non-nullable model fields can be `msgspec.UNSET`, distinct from
  an explicit JSON `null`.
- An optional JSON body may be absent; explicit `null` is accepted only when its
  schema is nullable. The handler receives `None` for an absent body.
- Multipart bodies require `multipart/form-data`. An absent required body is an
  error even if every field is optional. A present empty multipart object is valid
  when no fields are required. Form-encoded requests are not accepted as multipart.
- `additionalProperties: false` on an object without declared properties permits
  only an empty object.
- Validation errors return HTTP 422 with a `detail` list and the input location.
  msgspec reports the first error.
- Multiple object types in a decoded union need a supported tagged discriminator.
  Incompatible codecs fail generation before existing output files are updated.

See [the reproducible Uvicorn benchmark](benchmarks/README.md) for measured
throughput, latency, memory, and the distinction between checked/trusted responses.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run basedpyright
uv build
```

The parser and renderer are organized by responsibility:

- `parser/document.py` coordinates document validation and operation parsing. The other
  parser modules handle references, schemas, parameters, security, bodies, and responses.
- `render/router.py` emits explicit request parsing, handler calls and response encoding.
  `render/runtime.py` provides the generated HTTP helpers; contracts and authorization
  are rendered in separate modules. The original OpenAPI document is copied as JSON.
- `ir.py` defines the shared contract between parsing and rendering. Parser modules do
  not depend on the renderer; renderer modules consume the IR without parsing documents.
- Tests are grouped by behavior. Shared fixtures create isolated generated packages;
  `tests/snapshots` records the complete output for both example specifications.

The existing `oapi_gen.parser` and `oapi_gen.render` entry points remain available.
Snapshot tests compare generated files and the manifest byte for byte. Update the
snapshots only when an intentional output change has been reviewed, including changes
to the generator version or example specifications.

The operation parsing and naming behavior is partly adapted from the MIT-licensed
`fastapi-code-generator`; its copyright notice is included in the package.
