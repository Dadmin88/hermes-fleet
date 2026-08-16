"""Behavior tests for Fleet's transport-independent domain models."""

from __future__ import annotations

import pytest


def test_node_config_maps_friendly_name_to_opaque_keryx_peer_id() -> None:
    """A node uses a friendly name and opaque Keryx identity, never a URL."""
    from hermes_fleet.models import NodeConfig, NodePolicy

    node = NodeConfig(
        name="alpha-node",
        peer_id="12D3KooWExamplePeer",
        tags=("linux", "gpu"),
        enabled=True,
        priority=10,
        policy=NodePolicy(allowed_operations=("fleet.health",)),
    )

    assert node.name == "alpha-node"
    assert node.peer_id == "12D3KooWExamplePeer"
    assert node.tags == ("gpu", "linux")
    assert node.policy.allowed_operations == ("fleet.health",)
    with pytest.raises(ValueError, match="peer_id"):
        NodeConfig(name="alpha", peer_id="has whitespace")
    with pytest.raises(ValueError, match="peer_id"):
        NodeConfig(name="alpha", peer_id="\u0000control")
    with pytest.raises(ValueError, match="name"):
        NodeConfig(name="not valid", peer_id="peer")
    with pytest.raises(ValueError, match="tag"):
        NodeConfig(name="alpha", peer_id="peer", tags=("not valid",))


def test_node_policy_rejects_unhashable_string_subclasses() -> None:
    """Operation normalization cannot leak set-construction type errors."""
    from hermes_fleet.models import NodePolicy

    UnhashableStr = type("UnhashableStr", (str,), {"__hash__": None})

    with pytest.raises(ValueError, match="allowed_operations"):
        NodePolicy(allowed_operations=(UnhashableStr("fleet.health"),))


@pytest.mark.parametrize("field", ("name", "tag"))
def test_node_config_rejects_string_subclasses_with_custom_normalization(
    field: str,
) -> None:
    """Identifier validation does not invoke overridden string methods."""
    from hermes_fleet.models import NodeConfig

    BadStrip = type("BadStrip", (str,), {"strip": lambda self: 1})
    kwargs = {"name": "alpha", "peer_id": "peer-alpha", "tags": ()}
    if field == "name":
        kwargs["name"] = BadStrip("alpha")
    else:
        kwargs["tags"] = (BadStrip("gpu"),)

    with pytest.raises(ValueError, match=field):
        NodeConfig(**kwargs)


@pytest.mark.parametrize("behavior", ("plain-subclass", "explosive-strip"))
def test_node_config_requires_an_exact_primitive_peer_id(behavior: str) -> None:
    """Peer IDs cannot invoke string-subclass normalization hooks."""
    from hermes_fleet.models import NodeConfig

    attributes = {}
    if behavior == "explosive-strip":

        def explode(self):
            raise RuntimeError("strip hook ran")

        attributes["strip"] = explode
    PeerId = type("PeerId", (str,), attributes)

    with pytest.raises(ValueError, match="peer_id must be a string"):
        NodeConfig(name="alpha", peer_id=PeerId("peer-alpha"))


def test_remote_output_requires_exact_primitive_text() -> None:
    """Untrusted output retains only exact primitive strings."""
    from hermes_fleet.models import RemoteOutput

    Text = type("Text", (str,), {})
    with pytest.raises(ValueError, match="remote output must be text"):
        RemoteOutput(Text("hello"))


def test_node_policy_accepts_provider_neutral_worker_file_secret_reference() -> None:
    from hermes_fleet.models import NodePolicy

    policy = NodePolicy(allowed_secret_references=("secret://worker/file/HERMES_AUTH",))

    assert policy.allowed_secret_references == ("secret://worker/file/HERMES_AUTH",)


def test_node_config_requires_an_exact_node_policy() -> None:
    """Nested policy subclasses are rejected at model construction."""
    from hermes_fleet.models import NodeConfig, NodePolicy

    Policy = type("Policy", (NodePolicy,), {})
    with pytest.raises(ValueError, match="policy must be a NodePolicy"):
        NodeConfig(name="alpha", peer_id="peer-alpha", policy=Policy())


def test_keryx_inventory_models_do_not_expose_legacy_direct_a2a_concepts() -> None:
    """The public domain surface remains peer-based and credential-free."""
    import importlib.util
    from dataclasses import fields

    import hermes_fleet.models as models
    from hermes_fleet.config import FleetConfig

    assert tuple(field.name for field in fields(models.NodeConfig)) == (
        "name",
        "peer_id",
        "tags",
        "enabled",
        "priority",
        "policy",
    )
    assert tuple(field.name for field in fields(FleetConfig)) == (
        "schema_version",
        "defaults",
        "nodes",
        "managed_targets",
    )
    for legacy_name in (
        "AgentCard",
        "NodeSnapshot",
        "NodeTaskResult",
        "FleetTransport",
    ):
        assert not hasattr(models, legacy_name)
    assert importlib.util.find_spec("hermes_fleet.transport") is None


@pytest.mark.parametrize(
    "field",
    (
        "max_deadline_seconds",
        "max_payload_bytes",
        "max_prompt_chars",
        "max_export_paths",
        "priority",
    ),
)
def test_models_require_exact_primitive_integers(field: str) -> None:
    """Numeric subclasses cannot invoke hooks or survive model validation."""
    from hermes_fleet.models import FleetDefaults, NodeConfig

    def explode(self, other):
        raise RuntimeError("numeric comparison hook ran")

    Metric = type(
        "Metric",
        (int,),
        {"__lt__": explode, "__gt__": explode},
    )
    with pytest.raises(ValueError, match=field):
        if field == "priority":
            NodeConfig(name="alpha", peer_id="peer-alpha", priority=Metric(1))
        else:
            FleetDefaults(**{field: Metric(1)})


@pytest.mark.parametrize("field", ("allowed_operations", "tags"))
def test_models_reject_container_subclasses_before_iteration(field: str) -> None:
    """Container subclasses cannot invoke iteration hooks in model validation."""
    from hermes_fleet.models import NodeConfig, NodePolicy

    class ExplosiveTuple(tuple):
        def __iter__(self):
            raise RuntimeError("iteration hook ran")

    with pytest.raises(ValueError, match=field):
        if field == "allowed_operations":
            NodePolicy(allowed_operations=ExplosiveTuple(("fleet.health",)))
        else:
            NodeConfig(
                name="alpha",
                peer_id="peer-alpha",
                tags=ExplosiveTuple(("gpu",)),
            )


def test_node_policy_digest_is_canonical_and_changes_with_authority() -> None:
    from hermes_fleet.models import NodePolicy

    first = NodePolicy(
        allowed_operations=("fleet.health", "fleet.hermes.run"),
        max_deadline_seconds=120,
    )
    equal = NodePolicy(
        allowed_operations=("fleet.health", "fleet.hermes.run"),
        max_deadline_seconds=120,
    )
    changed = NodePolicy(
        allowed_operations=("fleet.health",),
        max_deadline_seconds=120,
    )
    secret_changed = NodePolicy(
        allowed_operations=("fleet.health", "fleet.hermes.run"),
        allowed_secret_references=("secret://worker/env/OPENROUTER_API_KEY",),
        max_deadline_seconds=120,
    )

    assert first.content_hash == equal.content_hash
    assert first.content_hash.startswith("sha256:")
    assert changed.content_hash != first.content_hash
    assert secret_changed.content_hash != first.content_hash


def test_node_policy_normalizes_and_validates_secret_references() -> None:
    from hermes_fleet.models import NodePolicy

    policy = NodePolicy(
        allowed_secret_references=(
            "secret://worker/env/OPENROUTER_API_KEY",
            "secret://worker/env/OPENROUTER_API_KEY",
        )
    )
    assert policy.allowed_secret_references == (
        "secret://worker/env/OPENROUTER_API_KEY",
    )
    with pytest.raises(ValueError, match="allowed_secret_references"):
        NodePolicy(allowed_secret_references=("secret://worker/env/PATH",))
