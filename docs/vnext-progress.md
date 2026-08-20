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
| 11 | COMPLETE | [Phase 11 scoped persistent memory](vnext-phase11-scoped-persistent-memory.md); PR #147 merged and `main` CI green per the repo closure rule (merged implementation + green `main` CI closes the CLOSURE-GATED acceptance record). |
| 12 | COMPLETE | [Phase 12 context firewall](vnext-phase12-context-firewall.md); PR #148 merged and `main` CI green per the repo closure rule. |
| 13 | COMPLETE | [Phase 13 sensitive interception](vnext-phase13-interception.md); PR #149 merged and `main` CI green per the repo closure rule; Phase 14 owns the scoped reference store this phase's interception hook requires but does not create. |
| 14 | COMPLETE | [Phase 14 scoped Vault references](vnext-phase14-scoped-vault-references.md); Vault custody, Agent runtime redemption/injection, and Fleet authorization/temporary-handle seams are merged and closed, with Fleet PR #151 and exact post-merge `main` CI green. |
| 15 | COMPLETE | [Phase 15 scoped skill learning](vnext-phase15-scoped-skill-learning.md); principal-private inactive candidates, exact source-run provenance/envelopes, native Hermes background-review routing, and mixed-version capability gating are merged, with Fleet PR #152 and exact post-merge `main` CI green. |
| 16 | COMPLETE | [Phase 16 skill quarantine](vnext-phase16-skill-quarantine.md); deterministic risk classification, immutable content-addressed quarantine seals, tamper detection, and exact Agent capability pinning are merged, with Fleet PR #153 and exact post-merge `main` CI green. |
| 17 | COMPLETE | [Phase 17 skill verification](vnext-phase17-skill-verification.md); exact-hash verification attestations, real disposable Bubblewrap denial/resource proofs, strict non-authority invariants, and mixed-version capability gating are merged, with Agent PR #13, Fleet PR #154, and exact post-merge `main` CI green on both repositories. |
| 18 | COMPLETE | [Phase 18 memory/skill promotion](vnext-phase18-memory-skill-promotion.md); explicit scoped promotion, exact-hash approval, sanitization, Phase 17 skill re-verification, administrator-lineage enforcement, multi-hop scope proof, append-only history/rollback, conflict detection, zero authority widening, final Agent PRs #15/#16, Fleet PR #156, and exact post-merge `main` CI green on both repositories. |
| 19 | COMPLETE | [Phase 19 deterministic security event model](vnext-phase19-deterministic-security-events.md); immutable exact-request/event facts, exact target/network/deadline binding, bounded memory/skill and interception/quarantine evidence, and separate deterministic hard-deny records are merged, with Fleet PR #158 and exact post-merge `main` CI green. |
| 20 | COMPLETE | [Phase 20 Templar core](vnext-phase20-templar-core.md); exact Phase 19 event evaluation, authority-free ALLOW/DENY/REVIEW verdicts, evaluator/model/policy audit binding, stale/substitution rejection, fail-closed evaluator errors, deep-frozen context, and deterministic Fleet hard-deny precedence are merged, with Fleet PR #160 and exact post-merge `main` CI green. |
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
| 37 | NOT STARTED | Migration off legacy disposable-profile execution, including production-local execution activation (same-machine `fleet.hermes.run` must not route through Keryx; current FX8 path submits via `submit_execution_package` → `keryx.send_task` with no local branch — a legacy-path divergence from `vnext-foundation.md:179–186`, not yet production-activated by any earlier phase). |
| 38 | NOT STARTED | Final security gates. |
| 39 | NOT STARTED | Release. |

## Current entry point

**Next work begins at Phase 21 (Disposable Templar sandbox).**

Phases 0–20 are closed under the repository closure rule. Phase 20 is merged and verified on Fleet `main`; Templar now exists as an authority-free evaluator over exact Phase 19 security events, with deterministic Fleet deny precedence and exact verdict/request/event/policy/evaluator binding.

Phase 21 is the next unstarted phase. It may add the disposable sandbox needed for evaluator process isolation and hard timeout termination, but it must not implement Phase 22 pre-execution gate ordering or Phase 23 learning/promotion gating.

Do not resume an old later-phase worktree simply because it exists.
