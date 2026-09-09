"""Shared media type and default parameter serialization rules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..errors import GenerationError
from ..ir import ParameterLocation

_JSON_MEDIA_TYPE = re.compile(r"^application/(?:[a-zA-Z0-9.+-]+\+)?json$")


def validate_parameter_serialization(
    raw: Mapping[str, Any],
    location: ParameterLocation,
    wire_name: str,
    context: str,
) -> None:
    default_style = {"query": "form", "path": "simple", "header": "simple", "cookie": "form"}[
        location
    ]
    style = raw.get("style", default_style)
    default_explode = default_style == "form"
    explode = raw.get("explode", default_explode)
    if style != default_style or explode != default_explode or raw.get("allowReserved") is True:
        raise GenerationError(
            f"{context}: custom serialization for {location} parameter {wire_name!r} "
            "is not supported yet"
        )
