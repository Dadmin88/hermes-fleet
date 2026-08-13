# Hermes Fleet Documentation

This directory contains the durable public documentation for Hermes Fleet. It describes the current product contracts and operational boundaries rather than the chronology of how individual implementation milestones were completed.

## New to Hermes Fleet? Start here

1. [**Ecosystem map**](ecosystem.md) - the complete Hermes stack: Fleet, Nodescale, Keryx, Hermes Agent, Hermes Agency, Desktop, and the private-network substrate. Start here to understand what each repository owns and how work moves through the system.
2. [**Architecture**](architecture.md) - Fleet's authority model, request flow, state ownership, execution boundary, and implementation strategy.
3. [**Deployment**](deployment.md) - the concrete controller/worker/Keryx/Hermes service topology and rollout guidance.

If your main question is **"where does Hermes Agency fit, and how will Fleet find or place profiles?"**, read [Profile identity and placement](profile-placement.md).

## By topic

### Ecosystem and architecture

- [Ecosystem map](ecosystem.md) - repository responsibilities, trust chain, cross-component flows, operator mental model, and current-versus-planned capability map.
- [Architecture](architecture.md) - component authorities, exact-node request flow, local authorization, durable Fleet state, deadlines, and execution correlation.
- [Profile identity and placement](profile-placement.md) - installed profile observations, exact Agency V1 package identity, ready-carrier lookup, pinned Agency snapshots, placement candidates, and the current locate-or-place boundary.

### Membership, state, and readiness

- [Managed projection V1](managed-projection-v1.md) - authenticated local Nodescale-to-Fleet managed-state contract.
- [Node observations and scheduler readiness](node-readiness.md) - layered liveness, freshness, capacity, observed Hermes profiles, reason codes, and operator configuration.

### Operator experience

- [Operator CLI V1](operator-cli.md) - top-level structured inspection, exact execution, task status, and diagnostics.
- [Unified Setup V1](unified-setup.md) - idempotent controller checks and existing-device worker software convergence without authority creation.
- [Operator foundation](operator-foundation.md) - presentation-neutral application service, canonical policy, managed identity resolution, structured results/errors, and read-only diagnostics.
- [Fleet Desktop](desktop.md) - native Hermes Desktop packaging, Fleet Overview, Network/Membership operator surfaces, real-state projection, installation, and troubleshooting.
- [Fleet Canvas topology](canvas.md) - truthful node graph, stable local layout, controls, accessibility, workflow-editor boundaries, and edge provenance.
- [Exact-execution demo](demo-exact-execution.md) - safe repeatable recording sequence for the live graph, stable target, durable task, and truthful terminal timeline.

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
