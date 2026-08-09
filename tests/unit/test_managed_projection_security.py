"""Adversarial security regressions for Fleet managed projections."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

import pytest

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _request(**overrides: object) -> dict[str, Any]:
    request: dict[str, Any] = {
        "source": "nodescale",
        "network_id": "network-a",
        "device_id": "device-a",
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "content_hash": _HASH_A,
        "operation": "upsert",
        "generated_operations": ("fleet.health",),
        "provenance": {
            "source": "nodescale",
            "network_id": "network-a",
            "device_id": "device-a",
            "snapshot": "1",
        },
    }
    return {**request, **overrides}


def _key() -> dict[str, str]:
    return {
        "source": "nodescale",
        "network_id": "network-a",
        "device_id": "device-a",
    }


def _provenance(**overrides: str) -> dict[str, str]:
    return {
        "source": overrides.get("source", "nodescale"),
        "network_id": overrides.get("network_id", "network-a"),
        "device_id": overrides.get("device_id", "device-a"),
        "snapshot": overrides.get("snapshot", "1"),
    }


def test_generated_execution_grant_is_rejected_not_silently_filtered(tmp_path) -> None:
    """A Nodescale projection must fail closed instead of dropping execution grants."""
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")

    with pytest.raises(ValueError):
        store.apply(
            **_request(generated_operations=("fleet.health", "fleet.hermes.run"))
        )

    assert store.inspect(**_key()) == {"generated": None, "effective": None}


def test_store_rejects_world_traversable_database_parent_without_creating_it(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    database_parent = tmp_path / "world-traversable"
    database_parent.mkdir(mode=0o755)
    database_parent.chmod(0o755)
    database_path = database_parent / "managed-projections.sqlite3"

    with pytest.raises(ValueError, match="database parent"):
        ManagedProjectionStore(database_path)

    assert not database_path.exists()


def test_store_rejects_symlinked_database_parent_without_creating_it(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    database_path = symlink_parent / "managed-projections.sqlite3"

    with pytest.raises(ValueError, match="database parent"):
        ManagedProjectionStore(database_path)

    assert not (real_parent / "managed-projections.sqlite3").exists()


def test_store_creates_private_same_uid_database_file_in_preprovisioned_parent(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    database_parent = tmp_path / "private"
    database_parent.mkdir(mode=0o700)
    database_parent.chmod(0o700)
    database_path = database_parent / "managed-projections.sqlite3"

    ManagedProjectionStore(database_path)

    identity = database_path.lstat()
    assert identity.st_uid == os.geteuid()
    assert identity.st_mode & 0o777 == 0o600


def test_unknown_generated_grant_is_rejected_not_silently_filtered(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")

    with pytest.raises(ValueError):
        store.apply(
            **_request(generated_operations=("fleet.health", "fleet.future.unknown"))
        )

    assert store.inspect(**_key()) == {"generated": None, "effective": None}


@pytest.mark.parametrize(
    ("field", "swapped_value"),
    (
        ("source", "other-controller"),
        ("network_id", "network-b"),
        ("device_id", "device-b"),
    ),
)
def test_provenance_identity_swaps_are_rejected_before_state_is_written(
    tmp_path, field: str, swapped_value: str
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    swapped_provenance = _provenance(**{field: swapped_value})

    with pytest.raises(ValueError):
        store.apply(**_request(provenance=swapped_provenance))

    assert store.inspect(**_key()) == {"generated": None, "effective": None}


@pytest.mark.parametrize(
    ("field", "invalid_generation"),
    (
        ("projection_generation", "01"),
        ("membership_generation", "01"),
        ("binding_generation", "01"),
        ("projection_generation", "18446744073709551616"),
    ),
)
def test_noncanonical_or_overflow_generation_is_rejected(
    tmp_path, field: str, invalid_generation: str
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")

    with pytest.raises(ValueError):
        store.apply(**_request(**{field: invalid_generation}))

    assert store.inspect(**_key()) == {"generated": None, "effective": None}


def test_same_generation_different_content_conflict_keeps_durable_generated_state(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"
    before = store.inspect(**_key())["generated"]

    result = store.apply(**_request(content_hash=_HASH_B))

    assert result.outcome == "conflict"
    assert store.inspect(**_key())["generated"] == before


def test_stale_upsert_cannot_resurrect_a_newer_tombstone(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"
    assert (
        store.apply(
            **_request(
                projection_generation="2",
                membership_generation="2",
                binding_generation="2",
                content_hash=_HASH_B,
                operation="remove",
                generated_operations=(),
                provenance=_provenance(snapshot="2"),
            )
        ).outcome
        == "applied"
    )
    tombstone = store.inspect(**_key())["generated"]

    result = store.apply(**_request())

    assert result.outcome == "stale"
    assert store.inspect(**_key())["generated"] == tombstone
    assert tombstone["state"] == "removed"


def test_local_deny_overrides_effective_state_without_mutating_generated_state(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert (
        store.apply(
            **_request(generated_operations=("fleet.health", "fleet.inventory"))
        ).outcome
        == "applied"
    )
    generated_before_deny = store.inspect(**_key())["generated"]

    store.set_operator_deny(**_key(), operation="fleet.inventory", denied=True)
    inspected = store.inspect(**_key())

    assert inspected["generated"] == generated_before_deny
    assert inspected["effective"]["allowed_operations"] == ("fleet.health",)
    assert inspected["effective"]["operator_denied_operations"] == ("fleet.inventory",)


def test_audit_read_window_is_bounded_across_adversarial_outcomes(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(
        tmp_path / "managed-projections.sqlite3", audit_limit=2
    )
    assert store.apply(**_request()).outcome == "applied"
    assert store.apply(**_request(content_hash=_HASH_B)).outcome == "conflict"
    assert (
        store.apply(
            **_request(
                projection_generation="2",
                membership_generation="2",
                binding_generation="2",
                content_hash="c" * 64,
                provenance=_provenance(snapshot="2"),
            )
        ).outcome
        == "applied"
    )
    assert store.apply(**_request()).outcome == "stale"

    with sqlite3.connect(store.path) as connection:
        audit_rows = connection.execute(
            "SELECT projection_generation, content_hash, outcome "
            "FROM managed_projection_audit ORDER BY sequence"
        ).fetchall()

    assert audit_rows == [
        ("2", "c" * 64, "applied"),
        ("1", _HASH_A, "stale"),
    ]
    assert len(audit_rows) == 2
    assert tuple(entry["outcome"] for entry in store.audit(**_key())) == (
        "applied",
        "stale",
    )


def test_direct_sql_cannot_update_or_delete_append_only_audit_evidence(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE managed_projection_audit "
                "SET outcome = 'stale' WHERE sequence = 1"
            )
        with pytest.raises(sqlite3.Error, match="immutable|no such function"):
            connection.execute(
                "DELETE FROM managed_projection_audit WHERE sequence = 1"
            )
        assert connection.execute(
            "SELECT outcome FROM managed_projection_audit WHERE sequence = 1"
        ).fetchone() == ("applied",)


@pytest.mark.parametrize(
    "unsafe_provenance",
    (
        (
            '{"source":"nodescale","network_id":"peer-network",'
            '"device_id":"peer-device","snapshot":"1","token":"raw-value"}'
        ),
        (
            r'{"source":"nodescale","network_id":"peer-network",'
            r'"device_id":"peer-device","snapshot":"1",'
            r'"\u0074oken":"raw-value"}'
        ),
        (
            '{"source":"nodescale","network_id":"peer-network",'
            '"device_id":"peer-device","snapshot":"1",'
            '"reason":"Bearer raw-value"}'
        ),
    ),
)
def test_direct_sql_rejects_secret_like_audit_provenance_but_allows_safe_peer_ids(
    tmp_path, unsafe_provenance: str
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    audit_values = (
        "nodescale",
        "peer-token-network",
        "peer-device",
        "1",
        "1",
        "1",
        _HASH_A,
        "active",
        '["fleet.health"]',
        '{"device_id":"peer-device","network_id":"peer-token-network","snapshot":"1","source":"nodescale"}',
        "applied",
    )

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO managed_projection_audit(
                source, network_id, device_id, projection_generation,
                membership_generation, binding_generation, content_hash, state,
                allowed_operations, provenance, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            audit_values,
        )
        with pytest.raises(sqlite3.IntegrityError, match="provenance"):
            connection.execute(
                """
                INSERT INTO managed_projection_audit(
                    source, network_id, device_id, projection_generation,
                    membership_generation, binding_generation, content_hash, state,
                    allowed_operations, provenance, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*audit_values[:-2], unsafe_provenance, audit_values[-1]),
            )
        assert connection.execute(
            "SELECT network_id, device_id FROM managed_projection_audit"
        ).fetchall() == [("peer-token-network", "peer-device")]


def test_direct_sql_rejects_neutral_key_provenance_that_could_carry_secrets(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    values = (
        "nodescale",
        "network-neutral",
        "device-neutral",
        "1",
        "1",
        "1",
        _HASH_A,
        "active",
        '["fleet.health"]',
        '{"device_id":"device-neutral","network_id":"network-neutral",'
        '"note":"sensitive-value","source":"nodescale"}',
        "applied",
    )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="provenance"):
            connection.execute(
                """
                INSERT INTO managed_projection_audit(
                    source, network_id, device_id, projection_generation,
                    membership_generation, binding_generation, content_hash, state,
                    allowed_operations, provenance, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
