from __future__ import annotations

from pathlib import Path

import pytest

from hermes_fleet.principal_identity import (
    SOURCE_LOCAL_PEER,
    PrincipalBinding,
    PrincipalDefinition,
    PrincipalRecord,
    PrincipalRegistry,
    derive_scoped_principal,
)
from hermes_fleet.promotion import (
    PromotionError,
    PromotionScopeRef,
    authorize_promotion,
)


def HASH(char: str) -> str:
    return "sha256:" + char * 64


def owner_and_administrator(
    tmp_path: Path,
    kind: str,
    scope_id: str,
) -> tuple[PrincipalRecord, PrincipalRecord]:
    registry = PrincipalRegistry(tmp_path / "principals.sqlite", now_ms=lambda: 1_000)
    owner_definition = PrincipalDefinition(
        kind="owner",
        subject="owner:kyle",
        scope={"owner": "kyle"},
    )
    owner, _created = registry.ensure(
        owner_definition,
        PrincipalBinding(
            source=SOURCE_LOCAL_PEER,
            evidence={"machine_id": "katana", "uid": 1000},
        ),
    )
    if kind == "owner":
        return owner, owner
    reference = derive_scoped_principal(
        registry,
        parent=owner.reference,
        kind=kind,
        subject=f"admin:{kind}:{scope_id}",
        scope={kind: scope_id},
    )
    return owner, registry.require_current(reference)


def independent_administrator(
    tmp_path: Path,
    kind: str,
    scope_id: str,
) -> PrincipalRecord:
    registry = PrincipalRegistry(tmp_path / "independent.sqlite", now_ms=lambda: 1_000)
    definition = PrincipalDefinition(
        kind=kind,
        subject=f"independent:{kind}:{scope_id}",
        scope={kind: scope_id},
    )
    record, _created = registry.ensure(
        definition,
        PrincipalBinding(
            source=SOURCE_LOCAL_PEER,
            evidence={"machine_id": "katana", "uid": 2000},
        ),
    )
    return record


def test_memory_promotion_binds_exact_sanitized_hash_and_has_no_authority(
    tmp_path: Path,
) -> None:
    owner, admin = owner_and_administrator(tmp_path, "project", "fleet")
    authorization = authorize_promotion(
        subject_kind="memory",
        subject_key="memory:" + HASH("3"),
        source_owner_principal_id=owner.reference.principal_id,
        agent_instance_id=HASH("2"),
        source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
        target_scope=PromotionScopeRef("project", "fleet"),
        source_content_hash=HASH("3"),
        approved_content_hash=HASH("4"),
        administrator=admin,
        now_ms=10_000,
        ttl_ms=60_000,
    )

    request = authorization.to_request()
    assert request["source_content_hash"] == HASH("3")
    assert request["approved_content_hash"] == HASH("4")
    assert request["authority"] == "none"
    assert request["administrator"]["principal_id"] == admin.reference.principal_id
    assert request["promotion_id"].startswith("sha256:")
    assert authorization.promotion_id == request["promotion_id"]


def test_skill_promotion_requires_exact_phase17_verification_digest(
    tmp_path: Path,
) -> None:
    owner, admin = owner_and_administrator(tmp_path, "network", "mesh-a")
    with pytest.raises(PromotionError, match="verification"):
        authorize_promotion(
            subject_kind="skill",
            subject_key=HASH("8"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
            target_scope=PromotionScopeRef("network", "mesh-a"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("3"),
            administrator=admin,
            now_ms=10_000,
        )

    authorization = authorize_promotion(
        subject_kind="skill",
        subject_key=HASH("8"),
        source_owner_principal_id=owner.reference.principal_id,
        agent_instance_id=HASH("2"),
        source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
        target_scope=PromotionScopeRef("network", "mesh-a"),
        source_content_hash=HASH("3"),
        approved_content_hash=HASH("3"),
        administrator=admin,
        verification_digest=HASH("5"),
        now_ms=10_000,
    )
    assert authorization.verification_digest == HASH("5")


def test_only_exact_target_scope_administrator_can_promote(tmp_path: Path) -> None:
    owner, wrong_project = owner_and_administrator(tmp_path, "project", "other")
    with pytest.raises(PromotionError, match="not an administrator"):
        authorize_promotion(
            subject_kind="memory",
            subject_key="memory:" + HASH("3"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
            target_scope=PromotionScopeRef("project", "fleet"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("4"),
            administrator=wrong_project,
            now_ms=10_000,
        )


def test_private_promotion_rejects_unrelated_target_administrator(
    tmp_path: Path,
) -> None:
    owner, _derived = owner_and_administrator(tmp_path, "project", "fleet")
    unrelated = independent_administrator(tmp_path, "project", "fleet")
    with pytest.raises(PromotionError, match="not derived from the source principal"):
        authorize_promotion(
            subject_kind="memory",
            subject_key="memory:" + HASH("3"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
            target_scope=PromotionScopeRef("project", "fleet"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("4"),
            administrator=unrelated,
            now_ms=10_000,
        )


def test_shared_scope_promotion_uses_exact_broader_scope_administrator(
    tmp_path: Path,
) -> None:
    owner, network_admin = owner_and_administrator(
        tmp_path, "network", "mesh-a"
    )
    authorization = authorize_promotion(
        subject_kind="memory",
        subject_key="memory:" + HASH("3"),
        source_owner_principal_id=owner.reference.principal_id,
        agent_instance_id=HASH("2"),
        source_scope=PromotionScopeRef("project", "fleet"),
        target_scope=PromotionScopeRef("network", "mesh-a"),
        source_content_hash=HASH("3"),
        approved_content_hash=HASH("4"),
        administrator=network_admin,
        now_ms=10_000,
    )
    assert authorization.source_scope == PromotionScopeRef("project", "fleet")
    assert authorization.target_scope == PromotionScopeRef("network", "mesh-a")
    assert authorization.administrator == network_admin.reference
    assert authorization.to_request()["authority"] == "none"


def test_promotion_must_widen_scope_and_private_source_must_match_owner(
    tmp_path: Path,
) -> None:
    owner, admin = owner_and_administrator(tmp_path, "project", "fleet")
    with pytest.raises(PromotionError, match="private source"):
        authorize_promotion(
            subject_kind="memory",
            subject_key="memory:" + HASH("3"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("principal", HASH("9")),
            target_scope=PromotionScopeRef("project", "fleet"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("4"),
            administrator=admin,
            now_ms=10_000,
        )

    with pytest.raises(PromotionError, match="broader"):
        authorize_promotion(
            subject_kind="memory",
            subject_key="memory:" + HASH("3"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("network", "mesh-a"),
            target_scope=PromotionScopeRef("project", "fleet"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("4"),
            administrator=admin,
            now_ms=10_000,
        )


def test_promotion_authorization_is_short_lived(tmp_path: Path) -> None:
    owner, admin = owner_and_administrator(tmp_path, "owner", "kyle")
    with pytest.raises(PromotionError, match="TTL"):
        authorize_promotion(
            subject_kind="memory",
            subject_key="memory:" + HASH("3"),
            source_owner_principal_id=owner.reference.principal_id,
            agent_instance_id=HASH("2"),
            source_scope=PromotionScopeRef("principal", owner.reference.principal_id),
            target_scope=PromotionScopeRef("owner", "kyle"),
            source_content_hash=HASH("3"),
            approved_content_hash=HASH("4"),
            administrator=admin,
            now_ms=10_000,
            ttl_ms=60 * 60 * 1000,
        )
