# Hermes Fleet Documentation

This directory contains the durable public documentation for Hermes Fleet. It describes the current product contracts and operational boundaries rather than the chronology of how individual implementation milestones were completed.

## Start here

- [Architecture](architecture.md) — component responsibilities, trust boundaries, request flow, state ownership, and implementation strategy.
- [Deployment](deployment.md) — generic service topology, configuration, rollout, verification, and rollback guidance.
- [Managed projection V1](managed-projection-v1.md) — authenticated local Nodescale-to-Fleet managed-state contract.
- [Node observations and scheduler readiness](node-readiness.md) — layered liveness, freshness, capacity, reason codes, and operator configuration.
- [Fleet Desktop D1](desktop.md) — native Hermes Desktop packaging, real-state projection, installation, and troubleshooting.
- [Integration verification](smoke-test.md) — repeatable two-node checks for direct communication, deliberate execution, deadlines, and trust boundaries.

## Other repository references

- [`../README.md`](../README.md) — project overview and quickstart.
- [`../SKILL.md`](../SKILL.md) — Hermes operator skill.
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history.
- [`../ops/`](../ops/) — service and deployment assets.

## Documentation policy

Public documentation should describe behavior that remains meaningful across machines and releases. Keep machine names, personal home paths, live task/run identifiers, temporary checkpoint hashes, one-off rollout evidence, and agent work logs in local workspace state, issues, pull requests, CI artifacts, or release notes instead of these reference documents.
