# Contributing

This guide covers development and releases of `oapi-gen` and `oapi-gen-dishka`.
For using the generator in an application, see the [README](README.md).

## Set up the workspace

Install Python 3.12+ and uv, clone the repository, and run all commands below from
the repository root:

```bash
uv sync --all-packages
```

The packages in `packages/*` share the root `uv.lock` and `.venv`.

The generator sources live in `packages/oapi-gen/src`; setuptools installs that
directory as `oapi_gen`. The workspace uses strict editable installation so type
checkers can resolve this mapping. After adding or renaming source modules,
refresh the installation:

```bash
uv sync --all-packages --reinstall-package oapi-gen
```

## Run checks

```bash
make test
make lint
make typecheck
uv build --all-packages
```

Use `make fmt` to sort imports and format Python code. CI runs tests, lint, and
type checks for both packages.

To work on a single package, pass its directory. For example, for Dishka:

```bash
uv run --all-packages --directory packages/oapi-gen-dishka pytest
uv run --all-packages --directory packages/oapi-gen-dishka ruff check .
uv run --all-packages --directory packages/oapi-gen-dishka basedpyright
uv build --package oapi-gen-dishka --out-dir dist
```

The Dishka package's workspace dependency on `oapi-gen` is only for integration
tests. Its published wheel does not depend on the generator at runtime.

## Code structure

The generator parses an OpenAPI document into an intermediate representation
(`ir.py`), then renders the Python package from it.

| Location | Responsibility |
| --- | --- |
| `packages/oapi-gen/src/parser/document.py` | Coordinates document validation and operation parsing. Other parser modules handle references, schemas, parameters, security, bodies, and responses. |
| `packages/oapi-gen/src/ir.py` | Defines the data shared by the parser and renderer. |
| `packages/oapi-gen/src/render/router.py` | Generates request parsing, handler calls, and response encoding. |
| `packages/oapi-gen/src/render/runtime.py` | Provides the generated HTTP helpers. Contracts and authorization have separate renderer modules. |
| `packages/oapi-gen-dishka/src/oapi_gen_dishka` | Integrates handler methods with Dishka's Starlette request scope. |
| `packages/*/tests` | Tests grouped by behavior, with fixtures that create isolated generated packages. |

Parser modules do not depend on the renderer. Renderer modules consume the IR
without parsing documents. The `oapi_gen.parser` and `oapi_gen.render` entry points
remain available.

Operation parsing and naming are partly adapted from the MIT-licensed
`fastapi-code-generator`; its copyright notice is included in the package.

## Generated output and snapshots

[Snapshot tests](packages/oapi-gen/tests/test_snapshots.py) compare the complete
generated file list, contents, and manifest byte for byte against
`packages/oapi-gen/tests/snapshots`.

Update snapshots only for intentional output changes, including changes to example
specifications. Generate the matching fixture into a temporary directory, review
the diff, then copy the output as described in the
[snapshot instructions](packages/oapi-gen/tests/snapshots/README.md).
Do not update snapshots just to make a structural refactor pass. A generator
version bump alone should not change them.

## Releases

The [release workflow](.github/workflows/release.yml) runs on a pushed `v<version>`
tag. The workspace and every package in `packages/*` must have that version in
their `pyproject.toml`. All packages are built and published together; use the
same version of `oapi-gen-dishka` as the generator that produced your code.

### Publisher setup

Configure a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
for each project (`oapi-gen` and `oapi-gen-dishka`):

| Field | Value |
| --- | --- |
| Owner | `mishamyrt` |
| Repository | `oapi-gen` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

For a new PyPI project, create a [pending publisher](https://pypi.org/manage/account/publishing/)
with the same values. Create the GitHub environment `pypi` and allow tags matching
`v*`. Publishing uses OIDC, without a PyPI token secret.

### Publish a version

Commit the code changes and set `VERSION` in the Makefile to the next release
version. Then run:

```bash
make publish
```

The command updates the workspace and package versions, refreshes the shared
lockfile and environment, and creates a release commit and an annotated `v<version>`
tag. It rejects an existing tag or uncommitted changes outside the Makefile.
Versions must use canonical Python spelling, for example `0.2.0rc1`.

Push the commit and tag to start the release workflow, replacing `0.2.0` with the
version you just set:

```bash
git push origin HEAD v0.2.0
```

CI checks both packages before publishing their wheel and source distributions.
The virtual workspace root is not published. After PyPI succeeds, the workflow
creates a GitHub Release with generated notes and the distributions attached.
Tags such as `v0.2.0rc1` or `v0.2.0.dev1` produce a GitHub prerelease.

If an upload fails partway through, rerun the failed jobs in the same workflow
run; already uploaded files are skipped. Publish subsequent changes under a new
shared version.
