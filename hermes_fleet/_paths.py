"""Internal trust predicates shared by filesystem-facing Fleet boundaries."""

from __future__ import annotations

from pathlib import Path

_CONCRETE_PATH_TYPE = type(Path())


def is_concrete_path(value: object) -> bool:
    """Return whether ``value`` is the exact platform Path implementation."""
    return type(value) is _CONCRETE_PATH_TYPE
