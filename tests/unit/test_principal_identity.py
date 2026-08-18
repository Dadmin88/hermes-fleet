from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_fleet.managed_projection import ManagedProjectionStore
from hermes_fleet.principal_identity import (
    PRINCIPAL_NETWORK,
    PRINCIPAL_OWNER,
    PRINCIPAL_PROJECT,
    PRINCIPAL_SERVICE,
    SOURCE_LOCAL_PEER,
    LocalPrincipalResolver,
    PrincipalBinding,
    PrincipalConflict,
    PrincipalDefinition,
    PrincipalError,
    PrincipalRegistry,
    PrincipalRevoked,
    RemoteDeviceBinding,
    RemotePrincipalResolver,
    derive_scoped_principal,
    get_local_peer_context,
    local_peer_scope,
)

HASH_1 = "sha256:" + "1" * 64


def registry(tmp_path: Path) -> PrincipalRegistry:
    return PrincipalRegistry(tmp_path / "principals.sqlite", now_ms=lambda: 1_000)


def owner_definition() -> PrincipalDefinition:
    return PrincipalDefinition(
        kind=PRINCIPAL_OWNER,
        subject="node-katana:uid:1000",
        scope={"owner": "node-katana:uid:1000"},
    )


def local_binding(uid: int = 1000) -> PrincipalBinding:
    return PrincipalBinding(
        source=SOURCE_LOCAL_PEER,
        evidence={"machine_id": "node-katana", "uid": uid},
    )


def test_local_owner_is_stable_across_registry_reopen_and_wrong_uid_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "principal-store" / "principals.sqlite"
    first = PrincipalRegistry(path, now_ms=lambda: 1_000)
    resolver = LocalPrincipalResolver(first, machine_id="node-katana", allowed_uid=1000)
    reference = resolver.resolve_owner(1000)
    assert reference.kind == PRINCIPAL_OWNER

    reopened = PrincipalRegistry(path, now_ms=lambda: 2_000)
    again = LocalPrincipalResolver(reopened, machine_id="node-katana", allowed_uid=1000)
    assert again.resolve_owner(1000) == reference
    assert (
        reopened.require_current(reference).definition.subject == "node-katana:uid:1000"
    )

    with pytest.raises(PrincipalError, match="uid"):
        again.resolve_owner(1001)


def test_local_peer_context_restores_and_is_thread_isolated(tmp_path: Path) -> None:
    resolver = LocalPrincipalResolver(
        registry(tmp_path), machine_id="node-katana", allowed_uid=1000
    )
    assert get_local_peer_context() is None
    with local_peer_scope(1000):
        assert get_local_peer_context().uid == 1000
        assert resolver.resolve_owner().kind == PRINCIPAL_OWNER
    assert get_local_peer_context() is None

    observed: list[int] = []

    def worker(uid: int) -> None:
        with local_peer_scope(uid):
            observed.append(get_local_peer_context().uid)

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in (1000, 1001)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(observed) == [1000, 1001]
    assert get_local_peer_context() is None


def test_rebind_and_revoke_increment_generation_and_stale_refs_fail(
    tmp_path: Path,
) -> None:
    store = registry(tmp_path)
    record, created = store.ensure(owner_definition(), local_binding())
    assert created is True
    first = record.reference

    rebound = store.rebind(
        first,
        PrincipalBinding(
            source=SOURCE_LOCAL_PEER,
            evidence={"machine_id": "node-katana-v2", "uid": 1000},
        ),
    )
    assert rebound.definition.principal_id == first.principal_id
    assert rebound.generation == first.generation + 1
    with pytest.raises(PrincipalConflict, match="stale"):
        store.require_current(first)

    revoked = store.revoke(rebound.reference)
    assert revoked.state == "revoked"
    assert revoked.generation == rebound.generation + 1
    with pytest.raises(PrincipalRevoked):
        store.require_current(revoked.reference)
    with pytest.raises(PrincipalRevoked):
        store.ensure(owner_definition(), revoked.binding)


def test_implicit_binding_change_fails_and_explicit_rebind_is_required(
    tmp_path: Path,
) -> None:
    store = registry(tmp_path)
    record, _ = store.ensure(owner_definition(), local_binding())
    changed = PrincipalBinding(
        source=SOURCE_LOCAL_PEER,
        evidence={"machine_id": "other-machine", "uid": 1000},
    )
    with pytest.raises(PrincipalConflict, match="rebind"):
        store.ensure(owner_definition(), changed)
    assert store.rebind(record.reference, changed).binding == changed


def test_scoped_principals_are_identity_only_and_parent_revocation_invalidates_child(
    tmp_path: Path,
) -> None:
    store = registry(tmp_path)
    owner, _ = store.ensure(owner_definition(), local_binding())
    project = derive_scoped_principal(
        store,
        parent=owner.reference,
        kind=PRINCIPAL_PROJECT,
        subject="project:fleet",
        scope={"project": "fleet"},
    )
    network = derive_scoped_principal(
        store,
        parent=project,
        kind=PRINCIPAL_NETWORK,
        subject="network:tailnet-prod",
        scope={"network": "tailnet-prod"},
    )
    service = derive_scoped_principal(
        store,
        parent=owner.reference,
        kind=PRINCIPAL_SERVICE,
        subject="service:builder",
        scope={"service": "builder"},
    )
    assert store.require_current(project).definition.scope == {"project": "fleet"}
    assert store.require_current(network).definition.scope == {
        "network": "tailnet-prod"
    }
    assert store.require_current(service).definition.scope == {"service": "builder"}
    assert set(project.to_dict()) == {
        "principal_id",
        "kind",
        "generation",
        "binding_hash",
    }

    store.revoke(owner.reference)
    for child in (project, network, service):
        with pytest.raises((PrincipalRevoked, PrincipalConflict)):
            store.require_current(child)


def test_principal_cannot_derive_from_itself(tmp_path: Path) -> None:
    store = registry(tmp_path)
    definition = PrincipalDefinition(
        kind=PRINCIPAL_PROJECT,
        subject="project:self",
        scope={"project": "self"},
    )
    binding = PrincipalBinding(
        source="scoped-parent",
        evidence={
            "parent_principal_id": definition.principal_id,
            "parent_generation": 1,
            "parent_binding_hash": HASH_1,
            "parent_kind": PRINCIPAL_PROJECT,
        },
    )
    with pytest.raises(PrincipalError, match="itself"):
        store.ensure(definition, binding)


def test_persisted_principal_shape_and_id_tampering_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "principals.sqlite"
    store = PrincipalRegistry(path, now_ms=lambda: 1_000)
    record, _ = store.ensure(owner_definition(), local_binding())

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE principals SET definition_json = ? WHERE principal_id = ?",
        ('{"kind":"owner"}', record.definition.principal_id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(PrincipalError, match="definition"):
        store.get(record.definition.principal_id)


NETWORK_ID = "11111111-1111-1111-1111-111111111111"
DEVICE_ID = "33333333-3333-3333-3333-333333333333"
PROVIDER_INSTANCE_ID = "22222222-2222-2222-2222-222222222222"
BINDING_ID = "44444444-4444-4444-4444-444444444444"


def _projection_store(tmp_path: Path) -> ManagedProjectionStore:
    store = ManagedProjectionStore(tmp_path / "projection.sqlite")
    store.apply(
        source="nodescale",
        network_id=NETWORK_ID,
        device_id=DEVICE_ID,
        projection_generation="5",
        membership_generation="3",
        binding_generation="4",
        content_hash="a" * 64,
        operation="upsert",
        generated_operations=("fleet.health",),
        provenance={
            "source": "nodescale",
            "network_id": NETWORK_ID,
            "device_id": DEVICE_ID,
            "snapshot": "5",
        },
    )
    return store


def _overview(*, online: bool = True, observed_id: str = HASH_1) -> dict:
    return {
        "schema": "nodescale.observations.v1",
        "network_id": NETWORK_ID,
        "reconciliation": {
            "state": "healthy",
            "last_attempted_at": "2026-08-18T03:00:00+00:00",
            "last_successful_at": "2026-08-18T03:00:00+00:00",
            "observed_count": 1,
        },
        "observations": [
            {
                "observed_id": observed_id,
                "network_id": NETWORK_ID,
                "provider_kind": "tailscale",
                "provider_instance_id": PROVIDER_INSTANCE_ID,
                "provider_node_id": "provider-node-1",
                "hostname": "compute-a",
                "given_name": "compute-a.example.invalid",
                "addresses": ["provider-address-1"],
                "tags": ["tag:fleet"],
                "registered_at": "2026-08-18T02:00:00+00:00",
                "last_seen_at": "2026-08-18T03:00:00+00:00",
                "expires_at": None,
                "online": online,
                "expired": False,
                "classification": "active",
                "first_observed_at": "2026-08-18T02:00:00+00:00",
                "last_observed_at": "2026-08-18T03:00:00+00:00",
                "snapshot_at": "2026-08-18T03:00:00+00:00",
            }
        ],
        "truncated": False,
    }


def _operator_device(
    *,
    peer_id: str = "peer-a",
    projection_generation: int = 5,
    membership_state: str = "active",
    trust_state: str = "trusted",
    binding_state: str = "active",
    revoked_at=None,
) -> dict:
    return {
        "device_id": DEVICE_ID,
        "network_id": NETWORK_ID,
        "display_name": "compute-a",
        "membership_state": membership_state,
        "roles": ["node", "worker"],
        "credential_generation": 3,
        "keryx_binding_generation": 4,
        "fleet_projection_generation": projection_generation,
        "fleet_projection_status": "applied",
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "provider_node_id": "provider-node-1",
        "durable_trust_state": trust_state,
        "durable_trust_revision": 7,
        "live_trust_evidence": "not_reconciled_by_operator_read",
        "provider_binding_state": "active",
        "provider_binding_revision": 8,
        "keryx_binding_id": BINDING_ID,
        "keryx_binding_state": binding_state,
        "verified_keryx_peer_id": peer_id,
        "keryx_binding_revision": 9,
        "live_keryx_binding_health": "not_exposed",
        "created_at": "2026-08-18T02:00:00+00:00",
        "updated_at": "2026-08-18T03:00:00+00:00",
        "revoked_at": revoked_at,
    }


def remote_binding() -> RemoteDeviceBinding:
    return RemoteDeviceBinding(nodescale_device_id=DEVICE_ID)


def test_remote_device_requires_keryx_operator_observation_and_projection_agreement(
    tmp_path: Path,
) -> None:
    principals = registry(tmp_path / "principal")
    projections = _projection_store(tmp_path)
    resolver = RemotePrincipalResolver(
        principals,
        projections,
        _overview,
        lambda device_id: _operator_device() if device_id == DEVICE_ID else {},
    )
    reference = resolver.resolve_device(
        authenticated_sender="peer-a",
        binding=remote_binding(),
    )
    record = principals.require_current(reference)
    assert record.definition.kind == "device"
    assert record.definition.scope == {
        "network": NETWORK_ID,
        "device": DEVICE_ID,
    }
    assert record.binding.evidence["keryx_peer_id"] == "peer-a"
    assert record.binding.evidence["keryx_binding_id"] == BINDING_ID
    assert "allowed_operations" not in record.binding.evidence
    assert "fleet_projection_generation" not in record.binding.evidence

    with pytest.raises(PrincipalError, match="not bound"):
        resolver.resolve_device(
            authenticated_sender="peer-other",
            binding=remote_binding(),
        )


def test_remote_identity_fails_closed_for_untrusted_or_stale_nodescale_state(
    tmp_path: Path,
) -> None:
    principals = registry(tmp_path / "principal")
    projections = _projection_store(tmp_path)

    cases = [
        (
            lambda: _overview(online=False),
            lambda _device: _operator_device(),
            "observation",
        ),
        (
            _overview,
            lambda _device: _operator_device(peer_id="peer-other"),
            "not bound",
        ),
        (_overview, lambda _device: _operator_device(trust_state="revoked"), "trusted"),
        (
            _overview,
            lambda _device: _operator_device(binding_state="revoked"),
            "trusted",
        ),
    ]
    for observation, operator, message in cases:
        resolver = RemotePrincipalResolver(
            principals,
            projections,
            observation,
            operator,
        )
        with pytest.raises(PrincipalError, match=message):
            resolver.resolve_device(
                authenticated_sender="peer-a",
                binding=remote_binding(),
            )

    unhealthy = _overview()
    unhealthy["reconciliation"]["state"] = "unreachable"
    resolver = RemotePrincipalResolver(
        principals,
        projections,
        lambda: unhealthy,
        lambda _device: _operator_device(),
    )
    with pytest.raises(PrincipalError, match="healthy"):
        resolver.resolve_device(
            authenticated_sender="peer-a",
            binding=remote_binding(),
        )

    stale_projection = RemotePrincipalResolver(
        principals,
        projections,
        _overview,
        lambda _device: _operator_device(projection_generation=6),
    )
    with pytest.raises(PrincipalConflict, match="epoch"):
        stale_projection.resolve_device(
            authenticated_sender="peer-a",
            binding=remote_binding(),
        )


def test_remote_identity_binding_changes_when_identity_trust_revision_changes(
    tmp_path: Path,
) -> None:
    principals = registry(tmp_path / "principal")
    projections = _projection_store(tmp_path)
    first = RemotePrincipalResolver(
        principals,
        projections,
        _overview,
        lambda _device: _operator_device(),
    ).resolve_device(authenticated_sender="peer-a", binding=remote_binding())

    changed = _operator_device()
    changed["keryx_binding_revision"] = 10
    with pytest.raises(PrincipalConflict, match="explicit rebind"):
        RemotePrincipalResolver(
            principals,
            projections,
            _overview,
            lambda _device: changed,
        ).resolve_device(authenticated_sender="peer-a", binding=remote_binding())
    assert principals.require_current(first).reference == first


def test_concurrent_distinct_principals_remain_isolated(tmp_path: Path) -> None:
    store = registry(tmp_path)
    barrier = threading.Barrier(3)
    references: list[tuple[str, object]] = []
    errors: list[BaseException] = []

    def admit(label: str, uid: int) -> None:
        try:
            definition = PrincipalDefinition(
                kind=PRINCIPAL_OWNER,
                subject=f"node-{label}:uid:{uid}",
                scope={"owner": f"node-{label}:uid:{uid}"},
            )
            binding = PrincipalBinding(
                source=SOURCE_LOCAL_PEER,
                evidence={"machine_id": f"node-{label}", "uid": uid},
            )
            barrier.wait()
            record, created = store.ensure(definition, binding)
            assert created is True
            references.append((label, record.reference))
        except BaseException as error:  # pragma: no cover - diagnostic path
            errors.append(error)

    threads = [
        threading.Thread(target=admit, args=("alice", 1000)),
        threading.Thread(target=admit, args=("bob", 1001)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(references) == 2
    by_label = dict(references)
    assert by_label["alice"].principal_id != by_label["bob"].principal_id
    assert store.require_current(by_label["alice"]).definition.subject.endswith(
        "uid:1000"
    )
    assert store.require_current(by_label["bob"]).definition.subject.endswith(
        "uid:1001"
    )


def test_remote_identity_does_not_change_when_only_authority_projection_changes(
    tmp_path: Path,
) -> None:
    principals = registry(tmp_path / "principal")
    projections = _projection_store(tmp_path)
    first = RemotePrincipalResolver(
        principals,
        projections,
        _overview,
        lambda _device: _operator_device(projection_generation=5),
    ).resolve_device(authenticated_sender="peer-a", binding=remote_binding())

    projections.apply(
        source="nodescale",
        network_id=NETWORK_ID,
        device_id=DEVICE_ID,
        projection_generation="6",
        membership_generation="3",
        binding_generation="4",
        content_hash="b" * 64,
        operation="upsert",
        generated_operations=("fleet.message",),
        provenance={
            "source": "nodescale",
            "network_id": NETWORK_ID,
            "device_id": DEVICE_ID,
            "snapshot": "6",
        },
    )
    second = RemotePrincipalResolver(
        principals,
        projections,
        _overview,
        lambda _device: _operator_device(projection_generation=6),
    ).resolve_device(authenticated_sender="peer-a", binding=remote_binding())
    assert second == first
