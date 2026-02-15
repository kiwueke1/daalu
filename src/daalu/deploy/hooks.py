# src/daalu/deploy/hooks.py
from __future__ import annotations

from typing import Callable, Dict, Any

# A simple global hook registry. You can swap this later for a plugin system.
_HOOKS: Dict[str, Callable[..., None]] = {}


def register(name: str):
    """Decorator to register a hook by name."""
    def _wrap(fn: Callable[..., None]):
        _HOOKS[name] = fn
        return fn
    return _wrap


def get(name: str) -> Callable[..., None]:
    """Fetch a hook by name. Raises KeyError if not found."""
    return _HOOKS[name]


def has(name: str) -> bool:
    return name in _HOOKS
