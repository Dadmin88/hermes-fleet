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
| 2 | COMPLETE | [Phase 2 disposable OCI body](vnext-phase2-disposable-oci-body.md); canonical Fleet + Agent PRs/main CI, generic Fleet-owned workshop, fail-closed observed-state hardening, attach-only independent Hermes verification, and same-container cross-repo Docker proof. |
| 3 | COMPLETE | [Phase 3 workspace isolation](vnext-phase3-workspace-isolation.md); canonical Fleet + Agent PRs/main CI, bounded tmpfs zones, canonicalized authority-scoped project projections, immutable read staging, separately authorized disposable write copies, hardened declared artifact export, exact quiescence-gated destruction, cross-repo Docker proof, and N+1 zero residue. |
| 4 | COMPLETE | [Phase 4 network isolation](vnext-phase4-network-isolation.md); four authority-bound modes, provider-only offline workshop posture, exact DNS/IP/port grants, hardened internal-network gateway, execution-bound recovery/adoption checks, topology-enforced proxy non-bypass, independent Hermes Docker-network verification, audit, rebinding/management/lateral denial, and cross-repo real-Docker proof. |
| 5 | COMPLETE | [Phase 5 host-action broker](vnext-phase5-host-action-broker.md); structured logical verbs only, exact authority/policy/target binding, immutable request/evidence payloads, race-safe idempotency and budgets, pre-effect deadline recheck, sticky post-effect indeterminate semantics, narrowing-only advisory, and real local host-effect proof. |
| 6 | COMPLETE | [Phase 6 persistent Agent Instances](vnext-phase6-persistent-agent-instances.md); native persistent Hermes brain, immutable Agency-base verification, stricter durable run-state exclusion, lock-ordered state validation, cross-thread/process mutation fencing, concurrent Hermes/no-config-collision proof, disposable-body survival, fresh Fleet/Hermes process proofs, fsync durability, and real two-boot QEMU/KVM machine-restart proof with preserved learning. |
| 7 | COMPLETE | Run-scoped Hermes execution overrides. Canonical Agent-fork PR #5 is merged and green; Fleet reconciliation PR #142 pins that exact fork SHA and verifies the installed runtime seam. Canonical closure still depends on exact-head PR CI and resulting `main` CI evidence, not this label alone. |
| 8 | COMPLETE | [Phase 8 Run Capsule lifecycle](vnext-phase8-run-capsule-lifecycle.md); current-master-plan reconciliation separates logical Recipe and ResolvedRecipe identity, versions the exact Capsule spec, binds compiler/provenance plus optional exact Workflow step identity, fails closed on legacy Capsule shapes, and recovers ambiguous pre-run body creation only by exact plan identity. The closure-status commit still requires fresh exact-head PR CI and resulting `main` CI before this label is canonical evidence. |
| 8A | COMPLETE | [Phase 8A Workflow → Recipe compilation and discovery](vnext-phase8a-workflow-recipe-compilation.md); Workflow v2 adds compile-only Recipe Steps, deterministic Candidate/Validated/Resolved Recipe forms, complete CPU/RAM/GPU/storage/runtime/security requirements, provenance/unknown states, exact cache invalidation, adaptive proposals, GPU placement, and low-authority disposable discovery. PR #144 repaired head is green; the closure-status commit still requires fresh exact-head PR CI and resulting `main` CI before this label is canonical evidence. |
| 9 | COMPLETE | [Phase 9 principal identity](vnext-phase9-principal-identity.md); formal owner/project/network/device/service principals, durable generation-fenced rebind/revoke state, kernel-authenticated local UID resolution, Keryx sender ↔ Nodescale operator verified binding ↔ live observation ↔ managed-projection remote identity agreement, concurrent-principal isolation, and Run Capsule v3 exact principal references. PR #145 implementation head is green; the closure-status commit still requires fresh exact-head PR CI and resulting `main` CI before this label is canonical evidence. |
| 10 | COMPLETE | [Phase 10 immutable RunAuthority](vnext-phase10-immutable-run-authority.md); one content-addressed authority document binds exact principal/Agent/Recipe/destination/policy/capabilities/resources/isolation/grants and derives existing Capsule/network/filesystem/broker surfaces. Durable state separately fences replay, expiry, cancellation/revocation, principal invalidation and multi-hop monotonic narrowing. PR #146 implementation head is green; the closure-status commit still requires fresh exact-head PR CI and resulting `main` CI before this label is canonical evidence. |
| 11 | LOCKED | Scoped persistent memory remains blocked until Phase 10's closure-status PR head and resulting `main` merge commit both pass required CI. |
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

**Next work begins at Phase 9.**

Before implementing Phase 9:

1. read the exact principal-identity requirements from the operator-supplied master plan;
2. keep local execution local: use the locally authenticated principal/session identity directly and do not route same-machine identity through Nodescale/Keryx;
3. for remote work only, bind Nodescale machine/device trust and the authenticated Keryx sender to the projected principal;
4. never infer principal identity from prompt/model text and never let the model claim or replace identity;
5. define bounded owner/project/network/device/service principal forms plus revocation state without implementing the full Phase 10 RunAuthority early;
6. make the principal reference consumed by Phase 8 Capsules deterministic and independently verifiable;
7. prove concurrent principals cannot collide or substitute one another and revoked principals fail closed;
8. close every Phase 9 requirement before marking Phase 10 active.

Do not resume an old later-phase worktree simply because it exists.
