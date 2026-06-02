from __future__ import annotations

from typing import Any

__all__ = ["__version__", "app"]

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    if name == "app":
        from .server import app as _app

        return _app
    raise AttributeError(name)
