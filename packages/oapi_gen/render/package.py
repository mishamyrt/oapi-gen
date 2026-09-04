"""Render package exports and the lazy router import."""

from __future__ import annotations

from ..ir import (
    ApiSpec,
)
from .inspection import used_security_schemes
from .writer import Writer, generated_header


def render_init(spec: ApiSpec) -> str:
    writer = Writer()
    writer.line(generated_header(spec.source_hash))
    writer.require("typing", "TYPE_CHECKING")
    contract_names = (
        "Handlers, SecurityHandler, SecurityRejected" if used_security_schemes(spec) else "Handlers"
    )
    writer.line(f"from .contracts import {contract_names}")
    writer.line()
    writer.line("if TYPE_CHECKING:")
    writer.line("from .router import create_router as create_router", indent=1)
    writer.line()
    writer.line(f"API_INFO_TITLE = {spec.title!r}")
    writer.line(f"API_INFO_VERSION = {spec.api_version!r}")
    writer.line()
    exports = (
        '["API_INFO_TITLE", "API_INFO_VERSION", "Handlers", "SecurityHandler", '
        '"SecurityRejected", "create_router"]'
        if used_security_schemes(spec)
        else '["API_INFO_TITLE", "API_INFO_VERSION", "Handlers", "create_router"]'
    )
    writer.line(f"__all__ = {exports}")
    writer.line()
    writer.line()
    writer.line("def __getattr__(name: str) -> object:")
    writer.line("if name == 'create_router':", indent=1)
    writer.line("from .router import create_router", indent=2)
    writer.line("globals()[name] = create_router", indent=2)
    writer.line("return create_router", indent=2)
    writer.line("raise AttributeError(f'module {__name__!r} has no attribute {name!r}')", indent=1)
    return writer.render()
