# Hermes Fleet vNext progress

This is the canonical phase ledger for the Hermes Fleet vNext implementation program.

The program proceeds in numeric order from Phase 0 through Phase 39. Existing branches, worktrees, experiments, old proofs, or merged features are evidence candidates only. They do not make a later phase complete until that phase is reached, audited against the current master plan, and closed with current evidence.

Read [vNext foundation](vnext-foundation.md) before this ledger.

## Execution rules

- Do not skip unfinished required work to reach a later phase.
- Do not mark a phase complete because another thread previously touched it.
- Reuse old work only after reconciling it to the frozen vNext architecture.
- Preserve behavior-level reliability and security guarantees even when replacing old plumbing.
- Same-machine work stays on local Fleet + Hermes primitives.
- Nodescale/Keryx enter only for actual inter-machine identity, trust, transport, reconciliation, or distributed coordination.
- Persistent Hermes Agent Instances are durable brain state, never disposable per-run state.
- Temporary execution power belongs to immutable RunAuthority and temporary Run Capsules, never durable profile configuration.

## Phase ledger

| Phase | Status | Acceptance record |
| ---: | --- | --- |
| 0 | COMPLETE | [vNext foundation](vnext-foundation.md); ownership, invariants, machine-boundary rule, canonical lifecycle, terminology, and read-first docs frozen. |
| 1 | COMPLETE | [Phase 1 reliability baseline](vnext-phase1-reliability-baseline.md); current Fleet + Agent reconciliation, upstream Nodescale socket fix, preserved 10-run soak. |
| 2 | NEXT | Disposable OCI runtime as the execution body. Audit the existing OCI backend and old sandbox experiments against the exact Phase 2 contract before changing implementation. |
| 3 | NOT STARTED | Workspace/filesystem isolation. |
| 4 | NOT STARTED | Network isolation. |
| 5 | NOT STARTED | Controlled host-action broker. |
| 6 | NOT STARTED | Persistent Hermes Agent Instances. |
| 7 | NOT STARTED | Run-scoped Hermes execution overrides. |
| 8 | NOT STARTED | Run Capsule lifecycle. |
| 9 | NOT STARTED | Principal identity. |
| 10 | NOT STARTED | Immutable RunAuthority. |
| 11 | NOT STARTED | Scoped persistent memory. |
| 12 | NOT STARTED | Context firewall. |
| 13 | NOT STARTED | Secret interception. |
| 14 | NOT STARTED | Scoped Vault references. |
| 15 | NOT STARTED | Scoped skill learning. |
| 16 | NOT STARTED | Skill quarantine. |
| 17 | NOT STARTED | Skill verification. |
| 18 | NOT STARTED | Memory/skill promotion. |
| 19 | NOT STARTED | Deterministic security event model. |
| 20 | NOT STARTED | Templar core. |
| 21 | NOT STARTED | Disposable Templar sandbox. |
| 22 | NOT STARTED | Templar pre-execution gate. |
| 23 | NOT STARTED | Templar learning/promotion gate. |
| 24 | NOT STARTED | Agency base + learned overlays. |
| 25 | NOT STARTED | Revocation and right-to-forget. |
| 26 | NOT STARTED | Audit/provenance. |
| 27 | NOT STARTED | Multi-user adversarial suite. |
| 28 | NOT STARTED | Positive persistent-learning suite. |
| 29 | NOT STARTED | Disposable-body proof. |
| 30 | NOT STARTED | Fault injection. |
| 31 | NOT STARTED | Keryx/Nodescale cross-machine self-healing. |
| 32 | NOT STARTED | Atomic deployment. |
| 33 | NOT STARTED | Remote Maintainer Challenge. |
| 34 | NOT STARTED | Multi-principal Maintainer Challenge. |
| 35 | NOT STARTED | Operator UX. |
| 36 | NOT STARTED | Hermes-GPT connector/operator reliability. |
| 37 | NOT STARTED | Migration off legacy disposable-profile execution. |
| 38 | NOT STARTED | Final security gates. |
| 39 | NOT STARTED | Release. |

## Current entry point

**Next work begins at Phase 2.**

Before implementing Phase 2:

1. read the exact Phase 2 requirements from the operator-supplied master plan;
2. inspect the current Fleet `ExecutionBackend`/Docker OCI implementation;
3. inspect historical Agent/Fleet Docker-sandbox work only as candidate material;
4. identify which Phase 2 requirements are already proven, partially implemented, conflicting with vNext, or absent;
5. close every Phase 2 requirement before marking Phase 3 active.

Do not resume an old later-phase worktree simply because it exists.
