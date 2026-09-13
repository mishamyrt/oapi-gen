"""Resolve internal JSON Pointers and reference alias chains."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..errors import GenerationError


class Resolver:
    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document

    def resolve_object(self, value: object, context: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise GenerationError(f"{context} must be an object")
        result = cast(dict[str, Any], value)
        seen: set[str] = set()
        while (ref := result.get("$ref")) is not None:
            if not isinstance(ref, str):
                raise GenerationError(f"{context}: $ref must be a string")
            if ref in seen:
                raise GenerationError(f"{context}: cyclic $ref alias {ref!r}")
            seen.add(ref)
            target = self.resolve_ref(ref, context)
            if not isinstance(target, dict):
                raise GenerationError(f"{context}: {ref!r} does not point to an object")
            result = {
                **cast(dict[str, Any], target),
                **{key: item for key, item in result.items() if key != "$ref"},
            }
        return result

    def resolve_ref(self, ref: object, context: str) -> object:
        if not isinstance(ref, str):
            raise GenerationError(f"{context}: $ref must be a string")
        if not ref.startswith("#/"):
            raise GenerationError(f"{context}: external $ref values are not supported: {ref!r}")

        current: object = self._document
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or part not in current:
                raise GenerationError(f"{context}: unresolved $ref {ref!r}")
            current = current[part]
        return current
