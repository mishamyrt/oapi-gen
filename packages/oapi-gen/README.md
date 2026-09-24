# oapi-gen

Generate Python 3.12+ server contracts and HTTP adapters from OpenAPI using
Starlette and msgspec. Implement the generated handler protocols and bind them
when creating the router.

```bash
uv add --dev oapi-gen
uv add starlette msgspec 'uvicorn[standard]'
uv run oapi-gen generate openapi.yaml --output app/http/generated
```

See the [documentation and examples](https://github.com/mishamyrt/oapi-gen#readme)
for the quick start and supported OpenAPI features. For development and releases,
see [CONTRIBUTING.md](https://github.com/mishamyrt/oapi-gen/blob/master/CONTRIBUTING.md).

The optional `oapi-gen-dishka` integration is released with the same version as
the generator.
