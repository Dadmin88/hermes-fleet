"""Stable presentation helpers for future Fleet results."""

from __future__ import annotations

from .models import RemoteOutput, _require_exact_type


def format_remote_output(output: RemoteOutput) -> str:
    """Label remote data explicitly without treating it as local instructions."""
    output = _require_exact_type(output, RemoteOutput, "output must be a RemoteOutput")
    return f"[UNTRUSTED REMOTE OUTPUT]\n{output.text}"
