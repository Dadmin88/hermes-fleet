"""Phase 12 authorization for Hermes pre-prompt context firewall."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_instance import AgentInstanceBinding
from .hermes_runs import HermesFleetContextBinding
from .principal_identity import PrincipalRecord
from .run_capsule import RunCapsuleSpec


class ContextFirewallError(RuntimeError):
    """Context firewall cannot be bound to the exact authorized run."""


@dataclass(frozen=True, slots=True)
class ContextFirewallAuthorization:
    """Fleet-owned Phase 12 authorization projected into Hermes."""

    binding: HermesFleetContextBinding


def authorize_context_firewall(
    spec: RunCapsuleSpec,
    principal: PrincipalRecord,
    agent: AgentInstanceBinding,
) -> ContextFirewallAuthorization:
    """Bind model-facing persisted context to exact current Fleet authority."""
    if type(spec) is not RunCapsuleSpec:
        raise ContextFirewallError("Run Capsule spec is invalid")
    if type(principal) is not PrincipalRecord:
        raise ContextFirewallError("principal record is invalid")
    if type(agent) is not AgentInstanceBinding:
        raise ContextFirewallError("Agent Instance binding is invalid")
    if principal.reference != spec.principal:
        raise ContextFirewallError("principal record does not match Run Capsule")
    if agent.instance_id != spec.agent_instance_id:
        raise ContextFirewallError("Agent Instance does not match Run Capsule")

    return ContextFirewallAuthorization(
        binding=HermesFleetContextBinding(
            principal_id=spec.principal.principal_id,
            principal_kind=spec.principal.kind,
            principal_generation=spec.principal.generation,
            principal_binding_hash=spec.principal.binding_hash,
            agent_instance_id=spec.agent_instance_id,
            base_manifest_digest=agent.base_manifest_digest,
            run_authority_hash=spec.run_authority_hash,
        )
    )
