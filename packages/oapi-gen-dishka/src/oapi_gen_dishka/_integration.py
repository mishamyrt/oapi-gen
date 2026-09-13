"""Expose the native Dishka HTTP container to nested handler calls."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from inspect import iscoroutinefunction
from typing import Any, cast

from dishka import AsyncContainer
from dishka.integrations.base import wrap_injection
from dishka.integrations.starlette import ContainerMiddleware
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

_request_container: ContextVar[AsyncContainer] = ContextVar(
    "oapi_gen_dishka_request_container"
)


def _get_container(args: tuple[Any, ...], kwargs: dict[str, Any]) -> AsyncContainer:
    try:
        return _request_container.get()
    except LookupError:
        raise RuntimeError(
            "An injected oapi-gen handler must run inside an HTTP request configured with "
            "oapi_gen_dishka.setup_dishka(container, app)."
        ) from None


def inject[Result](
    func: Callable[..., Result],
) -> Callable[..., Result]:
    """Inject FromDishka parameters using the active HTTP request's container.

    Runtime parameter annotations and the response type are preserved. Static
    typing allows arbitrary call arguments because injected parameters are removed;
    use the generated handler protocol for a strictly typed calling interface.
    """
    if not iscoroutinefunction(func):
        raise TypeError("oapi_gen_dishka.inject requires an async function or method")
    wrapped = wrap_injection(func=func, is_async=True, container_getter=_get_container)
    return cast(Callable[..., Result], wrapped)


class _RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _request_container.set(scope["state"]["dishka_container"])
        try:
            await self.app(scope, receive, send)
        finally:
            _request_container.reset(token)


def setup_dishka(container: AsyncContainer, app: Starlette) -> None:
    """Install native Dishka scope management and expose its container to handlers.

    Call once, before app startup, to install the request container and handler context.
    The application lifespan remains responsible for closing the APP container.
    """
    if not isinstance(container, AsyncContainer):
        raise TypeError("oapi_gen_dishka requires an AsyncContainer")
    if getattr(app.state, "dishka_container", None) is not None:
        raise RuntimeError(
            "Dishka is already configured for this app. Call oapi_gen_dishka.setup_dishka "
            "once, instead of the framework's native Dishka setup."
        )

    # Starlette prepends middleware: native Dishka must run before this bridge.
    app.add_middleware(_RequestContextMiddleware)
    app.add_middleware(ContainerMiddleware)
    app.state.dishka_container = container
