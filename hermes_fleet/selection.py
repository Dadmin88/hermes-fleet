"""Pure deterministic selection over locally configured Fleet inventory."""

from __future__ import annotations

from collections.abc import Iterable

from .models import NodeConfig


def _requested(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{label} must be strings")
    try:
        requested = tuple(values)
    except TypeError as error:
        raise ValueError(f"{label} must be strings") from error
    if not all(isinstance(value, str) for value in requested):
        raise ValueError(f"{label} must be strings")
    normalized = tuple(value.strip().lower() for value in requested)
    if any(not value for value in normalized):
        raise ValueError(f"{label} must not contain empty strings")
    return tuple(dict.fromkeys(normalized))


def select_nodes(
    nodes: Iterable[NodeConfig],
    *,
    names: Iterable[str] = (),
    tags: Iterable[str] = (),
) -> tuple[NodeConfig, ...]:
    """Return enabled configured targets by exact names or AND-matched tags."""
    configured = tuple(nodes)
    requested_names = _requested(names, "names")
    requested_tags = _requested(tags, "tags")
    if requested_names and requested_tags:
        raise ValueError("names and tags cannot be selected together")

    by_name = {node.name: node for node in configured}
    if requested_names:
        unknown_names = sorted(set(requested_names).difference(by_name))
        if unknown_names:
            raise ValueError(f"unknown node names: {', '.join(unknown_names)}")
        selected = tuple(by_name[name] for name in requested_names)
    elif requested_tags:
        configured_tags = {tag for node in configured for tag in node.tags}
        unknown_tags = sorted(set(requested_tags).difference(configured_tags))
        if unknown_tags:
            raise ValueError(f"unknown tags: {', '.join(unknown_tags)}")
        required_tags = set(requested_tags)
        selected = tuple(
            node for node in configured if required_tags.issubset(set(node.tags))
        )
    else:
        selected = configured

    return tuple(
        sorted(
            (node for node in selected if node.enabled),
            key=lambda node: (-node.priority, node.name),
        )
    )
