from __future__ import annotations

from pathlib import Path

import pytest
from oapi_gen import CheckFailedError, GenerationError, check_package, generate_package

FIXTURE = Path(__file__).parent / "fixtures" / "cats.openapi.yaml"


def test_generation_is_deterministic_and_check_detects_changes(tmp_path: Path) -> None:
    output = tmp_path / "generated_api"
    generate_package(FIXTURE, output)
    first = {path.name: path.read_bytes() for path in output.iterdir()}

    generate_package(FIXTURE, output)
    second = {path.name: path.read_bytes() for path in output.iterdir()}
    assert second == first
    check_package(FIXTURE, output)

    (output / "router.py").write_text("# stale\n", encoding="utf-8")
    with pytest.raises(CheckFailedError, match=r"router\.py"):
        check_package(FIXTURE, output)


def test_wraps_output_errors_as_generation_errors(tmp_path: Path, monkeypatch) -> None:
    import oapi_gen.generator as generator_module

    def fail_write(path: Path, content: str) -> None:
        raise OSError("read-only output")

    monkeypatch.setattr(generator_module, "_atomic_write", fail_write)

    with pytest.raises(GenerationError, match="cannot update generated package"):
        generator_module.generate_package(FIXTURE, tmp_path / "unwritable")


def test_rejects_invalid_generated_python_before_writing(tmp_path: Path, monkeypatch) -> None:
    import oapi_gen.generator as generator_module

    monkeypatch.setattr(generator_module, "format_python", lambda source: "def broken(:\n")

    with pytest.raises(GenerationError, match="invalid Python"):
        generator_module.generate_package(FIXTURE, tmp_path / "invalid_generated")

    assert not (tmp_path / "invalid_generated").exists()


def test_contract_docstrings_preserve_descriptions_and_escape_python(generate_api):
    import importlib

    description = 'Details with """ quotes, a newline\nand a backslash \\.'
    generated = generate_api(
        {
            "/value": {
                "get": {
                    "operationId": "test",
                    "summary": "Read a value",
                    "description": description,
                    "deprecated": True,
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "description": "Maximum number",
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "204": {
                            "description": "No content",
                            "headers": {
                                "X-Count": {
                                    "description": "Total count",
                                    "schema": {"type": "integer"},
                                }
                            },
                        }
                    },
                }
            }
        }
    )
    contracts = importlib.import_module(generated.__name__ + ".contracts")
    assert description in contracts.Test.Request.__doc__
    assert "limit: Maximum number" in contracts.Test.Request.__doc__
    assert "Read a value" in contracts.DefaultHandler.test.__doc__
    assert description in contracts.DefaultHandler.test.__doc__
    assert "Deprecated." in contracts.DefaultHandler.test.__doc__
    assert "No content" in contracts.Test.NoContent.__doc__
    assert "x_count: Total count" in contracts.Test.NoContent.__doc__
