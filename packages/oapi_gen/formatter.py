from __future__ import annotations

import black
import isort
from black.mode import TargetVersion


def format_python(source: str) -> str:
    sorted_source = isort.code(source, profile="black", line_length=100)
    return black.format_str(
        sorted_source,
        mode=black.Mode(
            line_length=100,
            target_versions={TargetVersion.PY312},
        ),
    )
