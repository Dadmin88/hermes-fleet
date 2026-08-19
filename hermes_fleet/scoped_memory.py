"""Phase 11 authorization for Hermes native scoped persistent memory."""

from __future__ import annotations

from dataclasses import dataclass

from .hermes_runs import HermesFleetMemoryBinding, HermesMemoryScopeRef
from .principal_identity import PrincipalRecord
from .run_capsule import RunCapsuleSpec

_MAX_READ_SCOPES = 16


class ScopedMemoryError(RuntimeError):
    """Scoped memory cannot be bound to the exact authorized run."""


@dataclass(frozen=True, slots=True)
class ScopedMemoryAuthorization:
    """Fleet-owned authorization result projected into Hermes's native store."""

    binding: HermesFleetMemoryBinding


def authorize_scoped_memory(
    spec: RunCapsuleSpec,
    principal: PrincipalRecord,
    *,
    retention_until_ms: int | None = None,
) -> ScopedMemoryAuthorization:
    """Derive a narrow Hermes memory binding from current Fleet authority.

    Principal-private memory is always readable and is the only writable scope.
    Shared scopes are read-only here and only become visible in Hermes after an
    explicit promotion has already marked their entries as promoted.
    """
    if type(spec) is not RunCapsuleSpec:
        raise ScopedMemoryError("Run Capsule spec is invalid")
    if type(principal) is not PrincipalRecord:
        raise ScopedMemoryError("principal record is invalid")
    if principal.reference != spec.principal:
        raise ScopedMemoryError("principal record does not match Run Capsule")
    if retention_until_ms is not None and (
        isinstance(retention_until_ms, bool)
        or type(retention_until_ms) is not int
        or retention_until_ms < 1
    ):
        raise ScopedMemoryError("memory retention deadline is invalid")

    private_scope = HermesMemoryScopeRef("principal", spec.principal.principal_id)
    candidates: list[HermesMemoryScopeRef] = [private_scope]

    # Project memory is explicitly run-authority scoped. A principal's durable
    # project membership alone does not widen a run whose RunAuthority omitted
    # that project.
    for project_id in spec.project_scope:
        candidates.append(HermesMemoryScopeRef("project", project_id))

    definition_scope = principal.definition.scope
    if "network" in definition_scope:
        candidates.append(HermesMemoryScopeRef("network", definition_scope["network"]))
    if "owner" in definition_scope:
        candidates.append(HermesMemoryScopeRef("owner", definition_scope["owner"]))

    # Agent Instance shared state is opt-in at the entry level: Hermes only
    # exposes promoted entries for non-principal scopes.
    candidates.append(HermesMemoryScopeRef("agent_instance", spec.agent_instance_id))

    read_scopes: list[HermesMemoryScopeRef] = []
    seen: set[tuple[str, str]] = set()
    for scope in candidates:
        key = (scope.kind, scope.scope_id)
        if key in seen:
            continue
        seen.add(key)
        read_scopes.append(scope)

    if len(read_scopes) > _MAX_READ_SCOPES:
        raise ScopedMemoryError("authorized memory scopes exceed Hermes bound")

    return ScopedMemoryAuthorization(
        binding=HermesFleetMemoryBinding(
            principal_id=spec.principal.principal_id,
            principal_kind=spec.principal.kind,
            principal_generation=spec.principal.generation,
            principal_binding_hash=spec.principal.binding_hash,
            agent_instance_id=spec.agent_instance_id,
            source_run=spec.execution_id,
            read_scopes=tuple(read_scopes),
            write_scope=private_scope,
            retention_until_ms=retention_until_ms,
        )
    )
