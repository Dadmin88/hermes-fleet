# Hermes Fleet Documentation

This directory contains the durable public documentation for Hermes Fleet. It describes the current product contracts and operational boundaries rather than the chronology of how individual implementation milestones were completed.

## New to Hermes Fleet? Start here

1. [**vNext foundation**](vnext-foundation.md) - the frozen planned ownership boundaries, hard invariants, machine-boundary rule, canonical Agent Instance/RunAuthority/Run Capsule lifecycle, and terminology.
2. [**Ecosystem map**](ecosystem.md) - the complete Hermes stack: Fleet, Nodescale, Keryx, Hermes Agent, Hermes Agency, Desktop, and the private-network substrate. Start here to understand what each repository owns and how work moves through the system today.
3. [**Architecture**](architecture.md) - Fleet's current authority model, request flow, state ownership, execution boundary, and implementation strategy.
4. [**Deployment**](deployment.md) - the concrete controller/worker/Keryx/Hermes service topology and rollout guidance.

If your main question is **"where does Hermes Agency fit, and how will Fleet find or place profiles?"**, read [Profile identity and placement](profile-placement.md).

## By topic

### Ecosystem and architecture

- [vNext foundation](vnext-foundation.md) - frozen planned ownership boundaries, hard invariants, machine-boundary rule, canonical lifecycle, and terminology.
- [vNext progress](vnext-progress.md) - canonical phase ledger and current entry point.
- [Phase 1 reliability baseline](vnext-phase1-reliability-baseline.md) - requirement-by-requirement preservation and cross-repository evidence for the proven reliability baseline.
- [Phase 2 disposable OCI body](vnext-phase2-disposable-oci-body.md) - Fleet-owned hardened workshop, generic tooling image boundary, deadline binding, independent Hermes verification, and real-Docker evidence.
- [Phase 3 workspace isolation](vnext-phase3-workspace-isolation.md) - canonicalized project projection, separate read/write authority, immutable non-root input staging, declared artifact export, scanning, and cross-run residue proof.
- [Phase 4 network isolation](vnext-phase4-network-isolation.md) - four explicit network modes, authority/DNS pinning, internal-only workshop topology, hardened Fleet egress gateway, proxy non-bypass, adversarial denial/audit proofs, and independent Hermes verification.
- [Phase 5 host-action broker](vnext-phase5-host-action-broker.md) - structured logical host verbs/targets, exact authority and node-policy validation, race-safe attempt budgets, sticky indeterminate idempotency, no generic shell/path/SSH/Docker/systemd surface, and structured effect evidence.
- [Phase 6 persistent Agent Instances](vnext-phase6-persistent-agent-instances.md) - stable Agency-based Hermes-native Agent identity, immutable base metadata, durable config integrity, memory/skill mutation generations, upgrade-required semantics, and fresh-process persistence proof.
- [Ecosystem map](ecosystem.md) - repository responsibilities, trust chain, cross-component flows, operator mental model, and current-versus-planned capability map.
- [Architecture](architecture.md) - component authorities, exact-node request flow, local authorization, durable Fleet state, deadlines, and execution correlation.
- [Profile identity and placement](profile-placement.md) - installed profile observations, exact Agency V1 package identity, ready-carrier lookup, pinned Agency snapshots, placement candidates, and the current locate-or-place boundary.

### Membership, state, and readiness

- [Managed projection V1](managed-projection-v1.md) - authenticated local Nodescale-to-Fleet managed-state contract.
- [Nodescale operator control V1](nodescale-operator-control.md) - strict read-only Fleet client for Nodescale-owned durable device authority.
- [Node observations and scheduler readiness](node-readiness.md) - layered liveness, freshness, capacity, observed Hermes profiles, reason codes, and operator configuration.

### Operator experience

- [Operator CLI V1](operator-cli.md) - top-level structured inspection, exact execution, task status, and diagnostics.
- [Unified Setup V1](unified-setup.md) - idempotent controller checks and existing-device worker software convergence without authority creation.
- [Operator foundation](operator-foundation.md) - presentation-neutral application service, canonical policy, managed identity resolution, structured results/errors, and read-only diagnostics.
- [Fleet Recipe contracts](recipes.md) - runtime-neutral logical requirements and exact immutable resolution identities; no backend or execution behavior.
- [Backend capability contracts](backend-capabilities.md) - provider-neutral hard eligibility guarantees without placement or runtime configuration.
- [ExecutionBackend contract](execution-backend.md) - idempotent provider-neutral realization lifecycle with explicit cleanup and indeterminate state.
- [Docker OCI execution backend](oci-backend.md) - first concrete mature-runtime adapter with digest-pinned immutable ingredients, hardened isolation, exact ownership, and response-loss recovery.
- [Durable execution instances](durable-execution-instances.md) - Fleet-owned generation-fenced correlation and recovery state without duplicating Keryx task/result authority.
- [Destination admission](destination-admission.md) - pure exact-target admission over current managed identity, policy, readiness, capacity, binding, and capability evidence.
- [Execution control API](execution-control-api.md) - destination-local authenticated bridge for admission, durable instance recovery, and generation-fenced lifecycle transitions.
- [Fleet Desktop](desktop.md) - native Hermes Desktop packaging, Fleet Overview, Network/Membership operator surfaces, real-state projection, installation, and troubleshooting.
- [Fleet Canvas topology](canvas.md) - truthful node graph, stable local layout, controls, accessibility, workflow-editor boundaries, and edge provenance.
- [Exact-execution demo](demo-exact-execution.md) - safe repeatable recording sequence for the live graph, stable target, durable task, and truthful terminal timeline.
- [Execution Fabric reconciliation](execution-fabric-reconciliation.md) - current shipped execution/workflow/profile boundaries and the exact FX1 contract scope.

### Operations and verification

- [Deployment](deployment.md) - generic service topology, configuration, rollout, verification, and rollback guidance.
- [Integration verification](smoke-test.md) - repeatable two-node checks for direct communication, deliberate execution, deadlines, and trust boundaries.
- [Private-network vertical-slice acceptance V1](acceptance/private-network-vertical-slice-v1.md) - sanitized checkpoint for the accepted two-machine exact-target Hermes execution path and its regression expectations.

## For coding agents

Repository-wide architecture and contribution instructions live in [`../AGENTS.md`](../AGENTS.md).

Agents should read the ecosystem map before making cross-component changes. In particular, preserve the separation between:

```text
private connectivity
!= device trust
!= Keryx authenticated identity
!= Fleet authorization
!= scheduler readiness
!= exact profile presence
!= execution permission
```

Do not promote open issues or design directions into current product documentation until the corresponding contracts and proofs are merged.

## Other repository references

- [`../README.md`](../README.md) - project overview and quickstart.
- [`../AGENTS.md`](../AGENTS.md) - coding-agent architecture and repository contract.
- [`../SKILL.md`](../SKILL.md) - Hermes operator skill.
- [`../CHANGELOG.md`](../CHANGELOG.md) - release history.
- [`../ops/`](../ops/) - service and deployment assets.

## Documentation policy

Public documentation should describe behavior that remains meaningful across machines and releases. Keep machine names, personal home paths, private network details, live task/run identifiers, temporary checkpoint hashes, one-off rollout evidence, and agent work logs in local workspace state, issues, pull requests, CI artifacts, or release notes instead of these reference documents.

When a document discusses architectural direction that is not yet a merged Fleet contract, label that distinction explicitly.
