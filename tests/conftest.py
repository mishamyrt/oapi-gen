"""Fixtures for isolated specification files and generated Python packages."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from oapi_gen import check_package, generate_package

from .support import ApiGenerator, PackageImporter, SpecWriter


@pytest.fixture
def write_specification(tmp_path: Path) -> SpecWriter:
    def write(name: str, source: str) -> Path:
        path = tmp_path / name
        path.write_text(source.lstrip(), encoding="utf-8")
        return path

    return write


@pytest.fixture
def import_generated(monkeypatch: pytest.MonkeyPatch) -> Iterator[PackageImporter]:
    packages: set[str] = set()

    def load(output: Path) -> ModuleType:
        packages.add(output.name)
        monkeypatch.syspath_prepend(str(output.parent))
        return importlib.import_module(output.name)

    yield load

    for name in list(sys.modules):
        if any(name == package or name.startswith(f"{package}.") for package in packages):
            sys.modules.pop(name, None)


@pytest.fixture
def generate_api(tmp_path: Path, import_generated: PackageImporter) -> ApiGenerator:
    def generate(
        paths: dict[str, Any],
        schemas: dict[str, Any] | None = None,
        *,
        validate_responses: bool = True,
        security_schemes: dict[str, Any] | None = None,
        media_types: dict[str, Any] | None = None,
        openapi_version: str = "3.1.0",
    ) -> ModuleType:
        name = f"generated_{uuid4().hex}"
        source = tmp_path / f"{name}.json"
        source.write_text(
            json.dumps(
                {
                    "openapi": openapi_version,
                    "info": {"title": "Contract boundaries", "version": "1"},
                    "paths": paths,
                    "components": {
                        "schemas": schemas or {},
                        "securitySchemes": security_schemes or {},
                        "mediaTypes": media_types or {},
                    },
                }
            )
        )
        output = tmp_path / name
        generate_package(source, output, validate_responses=validate_responses)
        check_package(source, output, validate_responses=validate_responses)
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F821", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return import_generated(output)

    return generate
