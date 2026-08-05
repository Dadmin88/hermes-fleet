"""Stable presentation helpers for future Fleet results."""

from __future__ import annotations

from .models import RemoteOutput


def format_remote_output(output: RemoteOutput) -> str:
    """Label remote data explicitly without treating it as local instructions."""
    return f"[UNTRUSTED REMOTE OUTPUT]\n{output.text}"
