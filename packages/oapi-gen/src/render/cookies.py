"""Framework-independent cookie values copied into generated packages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from http.cookies import SimpleCookie
from typing import Literal


@dataclass(frozen=True, slots=True, kw_only=True)
class Cookie:
    name: str
    value: str = ""
    max_age: int | None = None
    expires: datetime | None = None
    path: str | None = "/"
    domain: str | None = None
    secure: bool = False
    httponly: bool = False
    samesite: Literal["lax", "strict", "none"] | None = "lax"

    def to_header(self) -> str:
        cookie = SimpleCookie()
        cookie[self.name] = self.value
        morsel = cookie[self.name]
        attributes = {
            "max-age": self.max_age,
            "expires": format_datetime(self.expires, usegmt=True) if self.expires else None,
            "path": self.path,
            "domain": self.domain,
            "secure": self.secure,
            "httponly": self.httponly,
            "samesite": self.samesite,
        }
        for name, value in attributes.items():
            if value is not None:
                morsel[name] = value
        return morsel.OutputString()
