"""Focused durable-state tests for Fleet managed projections."""

from __future__ import annotations

import sqlite3

import pytest


def _request(**overrides):
    request = {
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
        "projection_generation": "10",
        "membership_generation": "20",
        "binding_generation": "30",
        "content_hash": "a" * 64,
        "operation": "upsert",
        "generated_operations": ("fleet.health",),
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": "10",
        },
    }
    return {**request, **overrides}


def _key():
    return {"source": "nodescale", "network_id": "network-1", "device_id": "device-1"}


def test_upsert_persists_generated_allowlist_and_full_provenance(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    result = store.apply(
        **_request(
            projection_generation="1",
            membership_generation="2",
            binding_generation="3",
            generated_operations=("fleet.inventory", "fleet.health"),
            provenance={
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "device-1",
                "controller": "nodescale",
                "snapshot": "1",
            },
        )
    )

    inspected = ManagedProjectionStore(store.path).inspect(**_key())

    assert result.outcome == "applied"
    assert inspected["generated"] == {
        "state": "active",
        "projection_generation": "1",
        "membership_generation": "2",
        "binding_generation": "3",
        "content_hash": "a" * 64,
        "allowed_operations": ("fleet.health", "fleet.inventory"),
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "controller": "nodescale",
            "snapshot": "1",
        },
    }
    assert inspected["effective"]["state"] == "active"
    assert inspected["effective"]["allowed_operations"] == (
        "fleet.health",
        "fleet.inventory",
    )


def test_replay_stale_conflict_and_newer_projection_are_distinguished(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    request = _request()

    assert store.apply(**request).outcome == "applied"
    assert store.apply(**request).outcome == "already_applied"
    assert store.apply(**_request(projection_generation="9")).outcome == "stale"
    assert store.apply(**_request(content_hash="b" * 64)).outcome == "conflict"
    assert (
        store.apply(
            **_request(
                projection_generation="11",
                content_hash="c" * 64,
                provenance={
                    "source": "nodescale",
                    "network_id": "network-1",
                    "device_id": "device-1",
                    "snapshot": "11",
                },
            )
        ).outcome
        == "applied"
    )
    assert store.inspect(**_key())["generated"]["projection_generation"] == "11"


def test_generation_gap_is_rejected_without_skipping_intervening_state(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"

    assert (
        store.apply(
            **_request(projection_generation="12", content_hash="b" * 64)
        ).outcome
        == "gap"
    )
    assert store.inspect(**_key())["generated"]["projection_generation"] == "10"


def test_remove_writes_a_tombstone_that_blocks_stale_resurrection(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"

    removed = store.apply(
        **_request(
            projection_generation="11",
            content_hash="b" * 64,
            operation="remove",
            generated_operations=(),
            provenance={
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "device-1",
                "snapshot": "11",
            },
        )
    )

    assert removed.outcome == "applied"
    assert store.inspect(**_key())["generated"]["state"] == "removed"
    assert store.apply(**_request()).outcome == "stale"
    assert (
        store.apply(
            **_request(
                projection_generation="12",
                content_hash="c" * 64,
                provenance={
                    "source": "nodescale",
                    "network_id": "network-1",
                    "device_id": "device-1",
                    "snapshot": "12",
                },
            )
        ).outcome
        == "applied"
    )


def test_operator_local_deny_is_separate_and_wins_over_regenerated_allowlist(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert (
        store.apply(
            **_request(
                generated_operations=(
                    "fleet.health",
                    "fleet.inventory",
                    "fleet.message",
                )
            )
        ).outcome
        == "applied"
    )

    store.set_operator_deny(**_key(), operation="fleet.inventory", denied=True)
    assert (
        store.apply(
            **_request(
                projection_generation="11",
                content_hash="b" * 64,
                generated_operations=(
                    "fleet.health",
                    "fleet.inventory",
                    "fleet.message",
                ),
                provenance={
                    "source": "nodescale",
                    "network_id": "network-1",
                    "device_id": "device-1",
                    "snapshot": "11",
                },
            )
        ).outcome
        == "applied"
    )

    inspected = store.inspect(**_key())
    assert inspected["generated"]["allowed_operations"] == (
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
    )
    assert inspected["effective"] == {
        "state": "active",
        "allowed_operations": ("fleet.health", "fleet.message"),
        "operator_denied_operations": ("fleet.inventory",),
    }


def test_audit_is_ordered_and_bounded_without_changing_projection_state(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(
        tmp_path / "managed-projections.sqlite3", audit_limit=2
    )
    assert store.apply(**_request()).outcome == "applied"
    assert store.apply(**_request()).outcome == "already_applied"
    assert store.apply(**_request(projection_generation="9")).outcome == "stale"

    audit = store.audit(**_key())
    assert tuple(entry["outcome"] for entry in audit) == ("already_applied", "stale")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM managed_projection_audit"
        ).fetchone() == (2,)
    restarted = ManagedProjectionStore(store.path, audit_limit=2)
    assert tuple(entry["outcome"] for entry in restarted.audit(**_key())) == (
        "already_applied",
        "stale",
    )
    assert restarted.inspect(**_key())["generated"]["projection_generation"] == "10"


def test_audit_retains_full_applied_provenance(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    store.apply(**_request())

    assert store.audit(**_key())[0] == {
        "sequence": 1,
        "projection_generation": "10",
        "membership_generation": "20",
        "binding_generation": "30",
        "content_hash": "a" * 64,
        "state": "active",
        "allowed_operations": ("fleet.health",),
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": "10",
        },
        "outcome": "applied",
        "created_at": store.audit(**_key())[0]["created_at"],
    }


def test_store_refuses_unknown_preexisting_sqlite_schema(tmp_path) -> None:
    import sqlite3

    import pytest

    from hermes_fleet.managed_projection import ManagedProjectionStore

    path = tmp_path / "managed-projections.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unexpected_state (value TEXT)")

    with pytest.raises(RuntimeError, match="schema is not ready"):
        ManagedProjectionStore(path)


def test_projection_input_rejects_noncanonical_generations_and_unsafe_grants(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")

    with pytest.raises(ValueError, match="projection_generation"):
        store.apply(**_request(projection_generation="01"))
    with pytest.raises(ValueError, match="generated_operations"):
        store.apply(**_request(generated_operations=("fleet.hermes.run",)))
    assert store.inspect(**_key()) == {"generated": None, "effective": None}


def test_newer_projection_rejects_membership_or_binding_counter_regression(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    assert store.apply(**_request()).outcome == "applied"
    before = store.inspect(**_key())["generated"]

    membership_regression = store.apply(
        **_request(
            projection_generation="11",
            membership_generation="19",
            binding_generation="31",
            content_hash="b" * 64,
            provenance={
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "device-1",
                "snapshot": "11",
            },
        )
    )
    binding_regression = store.apply(
        **_request(
            projection_generation="11",
            membership_generation="21",
            binding_generation="29",
            content_hash="c" * 64,
            provenance={
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "device-1",
                "snapshot": "11",
            },
        )
    )

    assert membership_regression.outcome == "regression"
    assert binding_regression.outcome == "regression"
    assert store.inspect(**_key())["generated"] == before


def test_store_rejects_sqlite_master_schema_text_that_only_preserves_table_names(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    path = tmp_path / "managed-projections.sqlite3"
    ManagedProjectionStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = sql || ' /* tampered */' "
            "WHERE type = 'table' AND name = 'managed_projections'"
        )
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RuntimeError, match="schema is not ready"):
        ManagedProjectionStore(path)


def test_direct_sql_rejects_noncanonical_or_unauthorized_generated_operations(
    tmp_path,
) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    values = (
        "nodescale",
        "network-1",
        "device-1",
        "1",
        "1",
        "1",
        "a" * 64,
        "active",
        '{"device_id":"device-1","network_id":"network-1","snapshot":"1","source":"nodescale"}',
    )
    with sqlite3.connect(store.path) as connection:
        for operations in (
            '["fleet.hermes.run"]',
            '["fleet.inventory","fleet.health"]',
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO managed_projections(
                        source, network_id, device_id, projection_generation,
                        membership_generation, binding_generation, content_hash, state,
                        allowed_operations, provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*values[:8], operations, values[8]),
                )
