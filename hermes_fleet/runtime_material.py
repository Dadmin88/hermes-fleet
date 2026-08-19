"""Phase 14 authorization for scoped Vault references and temporary run handles."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_secure_store import (
    PrincipalContext,
    RunContext,
    ScopeRef,
    VaultError,
    VaultStore,
    open_default_store,
)

from .hermes_runs import (
    HermesFleetVaultBinding,
    HermesRuntimeMaterialHandle,
)
from .principal_identity import PrincipalRecord
from .run_capsule import RunCapsuleSpec

_MAX_SCOPES = 32


class RuntimeMaterialError(RuntimeError):
    """Stable Fleet-owned error for scoped runtime material authorization."""


@dataclass(frozen=True, slots=True)
class RuntimeMaterialAuthorization:
    """Opaque run-handle binding produced from exact current Fleet authority."""

    binding: HermesFleetVaultBinding


def principal_context_for_run(
    spec: RunCapsuleSpec,
    principal: PrincipalRecord,
) -> PrincipalContext:
    """Project one current Fleet principal into the Vault scope contract.

    Project scope is deliberately derived only from the exact RunAuthority-backed
    Capsule. Durable principal membership alone cannot widen a run. Network and
    owner scopes come from the principal definition, matching the scoped-memory
    rule already established in Phase 11.
    """
    if type(spec) is not RunCapsuleSpec:
        raise RuntimeMaterialError("Run Capsule spec is invalid")
    if type(principal) is not PrincipalRecord:
        raise RuntimeMaterialError("principal record is invalid")
    if principal.reference != spec.principal:
        raise RuntimeMaterialError("principal record does not match Run Capsule")

    scopes: list[ScopeRef] = [ScopeRef("principal", spec.principal.principal_id)]
    for project_id in spec.project_scope:
        scopes.append(ScopeRef("project", project_id))

    definition_scope = principal.definition.scope
    if "network" in definition_scope:
        scopes.append(ScopeRef("network", definition_scope["network"]))
    if "owner" in definition_scope:
        scopes.append(ScopeRef("owner", definition_scope["owner"]))

    unique: list[ScopeRef] = []
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        key = (scope.kind, scope.scope_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(scope)
    if len(unique) > _MAX_SCOPES:
        raise RuntimeMaterialError("authorized runtime material scopes exceed bound")

    try:
        return PrincipalContext(
            principal_id=spec.principal.principal_id,
            principal_kind=spec.principal.kind,
            generation=spec.principal.generation,
            binding_hash=spec.principal.binding_hash,
            scopes=tuple(unique),
        )
    except ValueError as error:
        raise RuntimeMaterialError("Vault principal context is invalid") from error


def authorize_runtime_material(
    spec: RunCapsuleSpec,
    principal: PrincipalRecord,
    *,
    store: VaultStore | None = None,
) -> RuntimeMaterialAuthorization:
    """Mint temporary handles for exactly the RunAuthority-approved references."""
    context = principal_context_for_run(spec, principal)

    # Phase 14 is negotiated for every Fleet run. An empty handle set is still a
    # real binding, but it does not need to open/create a local Vault store.
    if not spec.secret_refs:
        return RuntimeMaterialAuthorization(
            binding=HermesFleetVaultBinding(
                run_id=spec.execution_id,
                run_authority_hash=spec.run_authority_hash,
                handles=(),
            )
        )

    custody = open_default_store() if store is None else store
    if not isinstance(custody, VaultStore):
        raise RuntimeMaterialError("runtime material store is invalid")

    try:
        minted = custody.mint_run_handles(
            spec.secret_refs,
            run=RunContext(
                principal=context,
                run_id=spec.execution_id,
                run_authority_hash=spec.run_authority_hash,
                deadline_ms=spec.deadline_ms,
            ),
        )
        handles = tuple(
            HermesRuntimeMaterialHandle(
                handle=item.handle,
                injection_kind=item.injection.kind,
                injection_target=item.injection.target,
                version=item.version,
                expires_at_ms=item.expires_at_ms,
            )
            for item in minted
        )
        return RuntimeMaterialAuthorization(
            binding=HermesFleetVaultBinding(
                run_id=spec.execution_id,
                run_authority_hash=spec.run_authority_hash,
                handles=handles,
            )
        )
    except (ValueError, VaultError) as error:
        raise RuntimeMaterialError(
            "runtime material references are not authorized for this run"
        ) from error


def revoke_runtime_material(
    spec: RunCapsuleSpec,
    *,
    store: VaultStore | None = None,
) -> int:
    """Revoke every temporary handle minted for the exact execution ID."""
    if type(spec) is not RunCapsuleSpec:
        raise RuntimeMaterialError("Run Capsule spec is invalid")
    if not spec.secret_refs:
        return 0
    custody = open_default_store() if store is None else store
    if not isinstance(custody, VaultStore):
        raise RuntimeMaterialError("runtime material store is invalid")
    try:
        return custody.revoke_run(spec.execution_id)
    except VaultError as error:
        raise RuntimeMaterialError("runtime material revocation failed") from error


__all__ = [
    "RuntimeMaterialAuthorization",
    "RuntimeMaterialError",
    "authorize_runtime_material",
    "principal_context_for_run",
    "revoke_runtime_material",
]
