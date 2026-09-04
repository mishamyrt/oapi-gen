"""Validate primitive values while preserving their OpenAPI error context."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..errors import GenerationError


def object_value(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GenerationError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def array_value(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise GenerationError(f"{context} must be an array")
    return cast(list[object], value)


def required_string(value: Mapping[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise GenerationError(f"{context}.{key} must be a non-empty string")
    return result


def optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GenerationError(f"{context} must be a string")
    return value
