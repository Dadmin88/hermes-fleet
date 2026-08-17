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
| 2 | COMPLETE | [Phase 2 disposable OCI body](vnext-phase2-disposable-oci-body.md); Fleet-owned generic workshop, hardening/deadline binding, observed Docker verification, Hermes attach-only independent verification, real-Docker proofs. |
| 3 | COMPLETE | [Phase 3 workspace isolation](vnext-phase3-workspace-isolation.md); canonicalized project projection, verified authority scope, distinct immutable read staging, separately authorized writable copies, declared/scanned artifact export, and real-Docker N+1 zero-residue proof. |
| 4 | COMPLETE | [Phase 4 network isolation](vnext-phase4-network-isolation.md); four explicit modes, offline provider traffic, authority/DNS pinning, internal-only workshop topology, hardened Fleet CONNECT gateway, proxy non-bypass, lateral/management/rebinding denial, audit, and independent Hermes verification. |
| 5 | COMPLETE | [Phase 5 host-action broker](vnext-phase5-host-action-broker.md); structured fixed verbs/targets, exact authority/policy/Recipe/target/parameter validation, race-safe budgets, sticky indeterminate idempotency, structured evidence, narrowing-only advisory seam, and real atomic host-effect proof. |
| 6 | COMPLETE | [Phase 6 persistent Agent Instances](vnext-phase6-persistent-agent-instances.md); stable Agency-based Hermes-native profile identity, exact-base reuse/upgrade-required semantics, durable config integrity, memory/skill generation locking, immutable-base inventory, concurrent creation, and fresh-process persistence proof. |
| 7 | COMPLETE | [Phase 7 run-scoped Hermes overrides](vnext-phase7-run-scoped-overrides.md); exact six-field `fleet_runtime`, ContextVar task/executor isolation, run-scoped toolset/iteration narrowing, attach-only exact image/container selection, live Docker proof, Hermes capability advertisement, and Fleet fail-closed capability gate. |
| 8 | NEXT | Run Capsule lifecycle. Orchestrate the temporary Fleet-owned execution state around the persistent Agent Instance and exact disposable body without deleting the Agent. |
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

**Next work begins at Phase 8.**

Before implementing Phase 8:

1. read the exact Run Capsule contents/lifecycle/recovery requirements from the operator-supplied master plan;
2. keep Run Capsule state Fleet-owned and temporary; it references but never deletes the persistent Agent Instance;
3. bind the Capsule to exact run/Agent/Recipe/authority/target/toolset/approval/secret/container/filesystem/network/broker/resource/deadline identities, while using verified placeholder authority slices until Phases 9–10 formalize principal and RunAuthority;
4. lifecycle order must be admit → ensure Agent Instance → create/start exact workshop → call Hermes native `/v1/runs` with the Phase 7 binding → wait → finalization/quiescence → verify evidence → persist only authorized learning → revoke temporary powers → destroy workshop → release handles → finalize Capsule;
5. make cleanup/recovery idempotent and recover by exact execution-plan identity; never silently create a replacement container while recovering an existing Capsule;
6. prove Agent Instance survives successful, failed, cancelled, and recovered Capsule cleanup;
7. preserve same-machine locality: no Keryx/Nodescale path for a local Capsule;
8. close every Phase 8 requirement before marking Phase 9 active.

Do not resume an old later-phase worktree simply because it exists.
