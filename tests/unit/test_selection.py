"""Tests for deterministic local Fleet target selection."""

from __future__ import annotations

from typing import Any, cast

import pytest


def test_selection_filters_enabled_nodes_by_all_tags_and_stable_priority_order() -> (
    None
):
    """Tag selection is local AND matching with deterministic order."""
    from hermes_fleet.models import NodeConfig
    from hermes_fleet.selection import select_nodes

    nodes = (
        NodeConfig(name="bravo", peer_id="peer-b", tags=("linux", "gpu"), priority=5),
        NodeConfig(name="alpha", peer_id="peer-a", tags=("linux", "gpu"), priority=5),
        NodeConfig(name="charlie", peer_id="peer-c", tags=("linux",), priority=99),
        NodeConfig(
            name="disabled", peer_id="peer-d", tags=("linux", "gpu"), enabled=False
        ),
    )

    selected = select_nodes(nodes, tags=("linux", "gpu"))

    assert [node.name for node in selected] == ["alpha", "bravo"]


def test_selection_reports_unknown_names_and_tags_instead_of_guessing() -> None:
    """Configured inventory is authoritative before later Keryx interaction."""
    from hermes_fleet.models import NodeConfig
    from hermes_fleet.selection import select_nodes

    nodes = (NodeConfig(name="alpha", peer_id="peer-a", tags=("linux",)),)

    with pytest.raises(ValueError, match="unknown node names: missing"):
        select_nodes(nodes, names=("missing",))
    with pytest.raises(ValueError, match="unknown tags: gpu"):
        select_nodes(nodes, tags=("gpu",))


def test_selection_rejects_mixed_name_and_tag_modes() -> None:
    """A request cannot ambiguously combine exact-name and tag selection."""
    from hermes_fleet.models import NodeConfig
    from hermes_fleet.selection import select_nodes

    nodes = (NodeConfig(name="alpha", peer_id="peer-a", tags=("linux",)),)

    with pytest.raises(ValueError, match="cannot be selected together"):
        select_nodes(nodes, names=("alpha",), tags=("linux",))


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    (
        ("names", "alpha", "must be strings"),
        ("tags", "linux", "must be strings"),
        ("names", (1,), "must be strings"),
        ("tags", (None,), "must be strings"),
        ("names", ("",), "must not contain empty strings"),
        ("tags", ("   ",), "must not contain empty strings"),
    ),
)
def test_selection_rejects_scalar_non_string_and_empty_selectors(
    keyword: str, value: object, message: str
) -> None:
    """Selector inputs fail closed instead of being coerced or iterated as text."""
    from hermes_fleet.models import NodeConfig
    from hermes_fleet.selection import select_nodes

    nodes = (NodeConfig(name="alpha", peer_id="peer-a", tags=("linux",)),)

    with pytest.raises(ValueError, match=message):
        cast(Any, select_nodes)(nodes, **{keyword: value})
