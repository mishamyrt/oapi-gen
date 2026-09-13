from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .errors import GenerationError
from .generator import check_package, generate_package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oapi-gen",
        description="Generate typed msgspec models and Starlette routers from OpenAPI.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("generate", "generate or update an output package"),
        ("check", "fail if an output package is missing or stale"),
    ):
        command_parser = subcommands.add_parser(command, help=help_text)
        command_parser.add_argument("spec", type=Path, help="OpenAPI YAML or JSON file")
        command_parser.add_argument(
            "--no-validate-responses",
            action="store_true",
            help="trust handler response values; input validation remains enabled",
        )
        command_parser.add_argument(
            "--output",
            "-o",
            type=Path,
            required=True,
            help="generated Python package directory",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            generate_package(
                arguments.spec,
                arguments.output,
                validate_responses=not arguments.no_validate_responses,
            )
            print(f"Generated {arguments.output}")
        else:
            check_package(
                arguments.spec,
                arguments.output,
                validate_responses=not arguments.no_validate_responses,
            )
            print(f"Generated package is up to date: {arguments.output}")
    except GenerationError as error:
        print(f"oapi-gen: error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
