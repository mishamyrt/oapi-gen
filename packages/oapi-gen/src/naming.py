"""Python name normalization adapted from fastapi-code-generator's parser."""

from __future__ import annotations

import keyword
import re
from functools import lru_cache

_NON_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


@lru_cache(maxsize=2048)
def snake_case(value: str) -> str:
    value = _NON_IDENTIFIER.sub("_", value)
    value = _ACRONYM_BOUNDARY.sub("_", value)
    value = _CAMEL_BOUNDARY.sub("_", value)
    value = re.sub(r"_+", "_", value).strip("_").lower()
    if not value:
        raise ValueError("name cannot be converted to a Python identifier")
    if value[0].isdigit():
        value = f"value_{value}"
    if keyword.iskeyword(value):
        value = f"{value}_"
    return value


@lru_cache(maxsize=2048)
def pascal_case(value: str) -> str:
    words = snake_case(value).strip("_").split("_")
    result = "".join(word[:1].upper() + word[1:] for word in words if word)
    if not result:
        raise ValueError("name cannot be converted to a Python class name")
    if result[0].isdigit():
        result = f"Value{result}"
    if keyword.iskeyword(result):
        result = f"{result}Model"
    return result


def available_name(preferred: str, used: set[str]) -> str:
    candidate = preferred
    suffix = 2
    while candidate in used:
        candidate = f"{preferred}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
