from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import CheckFailedError, GenerationError
from .formatter import format_python
from .models import generate_models
from .parser import parse_openapi
from .render import generated_header, render_contracts, render_init, render_router
from .render.contracts import render_contract_group, render_shared_contracts
from .render.inspection import has_cookie_arrays
from .render.router import render_routes

_MANIFEST = ".oapi-gen-manifest.json"


@dataclass(frozen=True, slots=True)
class RenderedPackage:
    source_hash: str
    files: dict[str, str]


def render_package(
    spec_path: Path,
    *,
    validate_responses: bool = True,
) -> RenderedPackage:
    spec_path = spec_path.expanduser().resolve()
    spec, document = parse_openapi(spec_path)
    header = generated_header()
    components = document.get("components", {})
    schemas = components.get("schemas", {})
    models_source = generate_models(spec_path, header, document) if schemas else f"{header}\n"
    sources = {
        "__init__.py": render_init(spec),
        "contracts/__init__.py": render_contracts(spec),
        "contracts/_shared.py": render_shared_contracts(spec),
        "models.py": models_source,
        "router.py": render_router(spec),
        "routes/__init__.py": header + "\n",
    }
    for group in spec.groups:
        grouped_spec = replace(spec, operations=group.operations, groups=(group,))
        sources[f"contracts/{group.field_name}.py"] = render_contract_group(grouped_spec)
        sources[f"routes/{group.field_name}.py"] = render_routes(
            grouped_spec, validate_responses=validate_responses
        )
    runtime = Path(__file__).parent / "render" / "runtime.py"
    sources["_runtime.py"] = header + "\n" + runtime.read_text(encoding="utf-8")
    if any(response.streaming for operation in spec.operations for response in operation.responses):
        streams = Path(__file__).parent / "render" / "streams.py"
        sources["_streams.py"] = (
            header
            + "\n"
            + streams.read_text(encoding="utf-8").replace(
                "from .runtime import", "from ._runtime import"
            )
        )
    if has_cookie_arrays(spec):
        cookies = Path(__file__).parent / "render" / "cookies.py"
        sources["_cookies.py"] = header + "\n" + cookies.read_text(encoding="utf-8")
    files = {name: _format_and_validate(name, source) for name, source in sources.items()}
    _validate_msgspec_package(files)
    # Stabilize declaration order without changing model fields or ordered OpenAPI arrays.
    document = {**document, "paths": dict(sorted(document["paths"].items()))}
    if schemas:
        document["components"] = {**components, "schemas": dict(sorted(schemas.items()))}
    files["openapi.json"] = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    return RenderedPackage(source_hash=spec.source_hash, files=files)


def generate_package(
    spec_path: Path,
    output: Path,
    *,
    validate_responses: bool = True,
) -> None:
    rendered = render_package(spec_path, validate_responses=validate_responses)
    output = output.expanduser().resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        previous_files = _read_manifest_files(output)

        for relative_path, content in rendered.files.items():
            _atomic_write(output / relative_path, content)

        stale_files = previous_files - rendered.files.keys()
        for relative_path in sorted(stale_files):
            candidate = _safe_generated_path(output, relative_path)
            if candidate.is_file():
                candidate.unlink()

        manifest = {
            "generator": "oapi-gen",
            "version": 1,
            "files": sorted(rendered.files),
        }
        _atomic_write(output / _MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except OSError as error:
        raise GenerationError(f"cannot update generated package {output}: {error}") from error


def check_package(
    spec_path: Path,
    output: Path,
    *,
    validate_responses: bool = True,
) -> None:
    rendered = render_package(spec_path, validate_responses=validate_responses)
    output = output.expanduser().resolve()
    stale: list[str] = []
    for relative_path, expected in rendered.files.items():
        path = output / relative_path
        try:
            actual = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            stale.append(relative_path)
        except OSError as error:
            raise GenerationError(f"cannot read generated file {path}: {error}") from error
        else:
            if actual != expected:
                stale.append(relative_path)

    manifest_files = _read_manifest_files(output)
    stale.extend(sorted(manifest_files - rendered.files.keys()))
    manifest_path = output / _MANIFEST
    if not manifest_path.is_file():
        stale.append(_MANIFEST)
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stale.append(_MANIFEST)
        else:
            if manifest.get("files") != sorted(rendered.files):
                stale.append(_MANIFEST)
    if stale:
        raise CheckFailedError(sorted(set(stale)))


def _validate_msgspec_package(files: dict[str, str]) -> None:
    # Resolve forward references and codec restrictions before updating any output files.
    with tempfile.TemporaryDirectory(prefix="oapi-gen-validate-") as directory:
        package = Path(directory) / "generated"
        package.mkdir()
        for name, content in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import generated.router, generated.models, msgspec\n"
                    "for value in vars(generated.models).values():\n"
                    "    if isinstance(value, type) and issubclass(value, msgspec.Struct):\n"
                    "        msgspec.json.Decoder(value)\n"
                ),
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode:
        raise GenerationError(
            "cannot build Starlette/msgspec codecs; check "
            "that the schema uses supported msgspec types "
            "(object unions require a discriminator):\n" + result.stderr.strip()
        )


def _read_manifest_files(output: Path) -> set[str]:
    path = output / _MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    except OSError as error:
        raise GenerationError(f"cannot read generated-file manifest {path}: {error}") from error
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise GenerationError(f"invalid generated-file manifest: {path}")
    return set(files)


def _safe_generated_path(output: Path, relative_path: str) -> Path:
    candidate = (output / relative_path).resolve()
    if output != candidate and output not in candidate.parents:
        raise GenerationError(f"unsafe path in generated-file manifest: {relative_path!r}")
    return candidate


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _format_and_validate(filename: str, source: str) -> str:
    try:
        formatted = format_python(source)
    except Exception as error:
        raise GenerationError(f"cannot format generated {filename}: {error}") from error
    try:
        compile(formatted, filename, "exec")
    except SyntaxError as error:
        raise GenerationError(f"generated {filename} is invalid Python: {error}") from error
    return formatted
