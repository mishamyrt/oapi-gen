"""Dishka injection for the ordinary async methods called by generated routers."""

from ._integration import inject, setup_dishka

__all__ = ["inject", "setup_dishka"]
