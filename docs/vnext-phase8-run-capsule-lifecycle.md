# Hermes Fleet vNext Phase 8 acceptance: Run Capsule lifecycle

Status: **COMPLETE**

Phase 8 composes the already-closed Phase 2–7 primitives into one temporary,
Fleet-owned Run Capsule lifecycle around a persistent Hermes Agent Instance.
The Capsule is disposable execution state. The Agent Instance remains the durable
brain and is never deleted as part of run cleanup.

This phase deliberately does not implement Phase 9 principal authentication or
Phase 10 RunAuthority issuance early. `principal_id` and `run_authority_hash` are
opaque, already-verified references carried by the Capsule. Later phases become
the authoritative producers/verifiers of those identities.

## Durable Capsule contract

`RunCapsuleSpec` is immutable, content-hashed, and serialized as the exact
`fleet.run-capsule-spec.v2` payload. It contains:

- execution/run identity and idempotency digest;
- persistent Agent Instance ID;
- principal ID reference;
- logical Recipe hash and exact ResolvedRecipe hash as separate identities;
- Recipe compiler/version identifier and exact requirement-provenance digest;
- optional all-or-nothing Workflow ID/revision/hash/step binding for later Phase 8A callers;
- RunAuthority hash reference;
- capabilities hash;
- canonical target object plus exact target digest;
- project scope;
- exact Phase 4 `NetworkGrant` plus network policy hash;
- `fleet-terminal` toolset and bounded iteration budget;
- approval budget;
- secret references only, never secret bodies;
- Phase 3 filesystem and artifact grants;
- Phase 5 structured host-broker grants;
- CPU, RAM and PID limits;
- absolute deadline;
- digest-pinned OCI image;
- exact execution-plan fingerprint;
- optional remote Keryx task identity for a later cross-machine caller.

Phase 8 binds these identities but does not implement the Phase 8A Workflow
compiler. Direct Recipe callers use an explicit compiler/source identifier such
as `fleet.recipe-direct.v1` plus a provenance digest. When a Workflow binding is
present, all four Workflow fields are required together; partial Workflow
identity fails closed. The body independently requires both
`ResolvedRecipe.recipe_hash == spec.recipe_hash` and
`ResolvedRecipe.content_hash == spec.resolved_recipe_hash`.

The local Phase 8 executor accepts only `none` and `provider-only` workshop
network modes. Provider traffic remains outside the workshop. The direct-egress
Phase 4 gateway remains independently proven, but is not widened into the narrow
Phase 7 `fleet_runtime` payload in this phase.

## Durable store and crash recovery

`RunCapsuleStore` is a SQLite generation-CAS store with:

- one exact content-hashed spec per execution ID;
- replay rejection when the same execution ID carries changed content;
- monotonic generation fencing on every transition;
- WAL mode and `synchronous=FULL`;
- owned, non-symlink database state with mode `0600`;
- safe parent-directory ownership/permission checks;
- bounded canonical JSON for spec/evidence;
- exact persisted spec shape validation; legacy/incomplete/unknown Capsule specs fail closed rather than being reinterpreted under the new Recipe identity semantics;
- only references/metadata, never secret bodies or artifact payload bytes.

The lifecycle state machine includes:

`admitted → agent_ready → body_ready → run_submitting → running`

followed by terminal/quiescence/evidence/learning/revocation/cleanup/finalization
states.

`run_submitting` is a deliberate crash fence. It is persisted before the Hermes
`/v1/runs` submission. If recovery sees `run_submitting` without a durable Hermes
run ID, the submission may already have reached Hermes, so Fleet marks the
outcome indeterminate and never reposts the run.

Likewise, Docker creation and recovery are different APIs:

- `create_initial()` may call the Phase 2 workshop `ensure()` path;
- `find_existing_by_plan()` and `recover_exact()` use `find()` only;
- recovery never calls `ensure()`;
- an existing body can be rediscovered by exact execution-plan identity;
- if initial body creation becomes indeterminate before the container ID is durably recorded, Fleet persists that exact stage and recovery may only rediscover the already-existing body by plan identity for no-run cleanup;
- that recovery path never calls `ensure()` or submits Hermes work, and it finalizes only after the rediscovered exact body is removed;
- a missing exact body is never silently replaced and an unobservable ambiguous creation remains indeterminate;
- cleanup is idempotent when the exact body is already gone.

This locks the master-plan requirement that recovery must not manufacture a new
container under the identity of an old Capsule.

## Canonical local lifecycle

The successful local lifecycle is now:

1. admit the immutable Capsule spec;
2. ensure/reopen the persistent Agent Instance;
3. create the exact hardened Fleet workshop;
4. durably bind the exact container ID and plan fingerprint;
5. persist `run_submitting`;
6. start Hermes through native `/v1/runs` with the Phase 7 `fleet_runtime`;
7. durably bind the exact Hermes run ID;
8. wait for terminal execution status;
9. call Hermes finalization and require exact-run `quiescent=true`;
10. validate process/command evidence;
11. export declared artifacts and durably hand them off before cleanup;
12. persist authorized learning, or an explicit policy skip;
13. revoke temporary approval/secret/broker powers;
14. enter `cleanup_pending`;
15. destroy the exact Fleet-owned container;
16. release the in-memory Hermes client handle;
17. mark the Capsule `cleaned`;
18. mark the Capsule `finalized`.

The persistent Agent Instance remains intact throughout.

## Evidence and artifacts

The default Phase 8 evidence verifier requires:

- the exact Hermes run ID;
- terminal Hermes status;
- `quiescent=true`;
- valid command evidence;
- zero pending processes.

Result text is represented durably by a SHA-256 digest rather than copied into
Capsule evidence by default.

Declared artifact bytes are exported from the live disposable body before
revocation/cleanup. If artifacts are declared, an explicit durable artifact
persister is mandatory. Only returned artifact metadata/hashes are written into
Capsule evidence; raw artifact bytes are not stored in the Capsule database.

If artifact persistence fails, the Capsule remains before `evidence_verified`
and the body is retained so recovery can retry the idempotent durable handoff.

## Learning and temporary-power revocation

Authorized learning is represented by an explicit persistence callback. When no
learning policy exists, Phase 8 records an explicit `skipped` decision rather
than inventing learning.

A Capsule carrying any of these powers:

- approval budget;
- secret references;
- host-broker grants;

cannot advance from `learning_persisted` to cleanup without an explicit grant
revoker. Tests prove both sides:

- missing revocation leaves the body intact and `grants_revoked=false`;
- successful revocation is persisted before body cleanup begins.

No authority is stored in Agent memory/skills by this lifecycle.

## Failure and recovery behavior

The focused suite proves:

- successful run: quiescence → evidence → learning → revocation → cleanup;
- deadline path: Hermes cancellation/finalization is quiesced, Capsule records
  `timed_out`, body is destroyed, Agent remains;
- definite pre-acceptance Hermes failure: body is safely cleaned, Agent remains;
- indeterminate body creation before durable container binding: the stage is persisted; recovery can rediscover only the exact plan-owned body and clean it without recreation or Hermes submission;
- submission response loss: Capsule becomes indeterminate and retains the exact
  body; the run is never reposted;
- finalization failure: body remains because quiescence is unproven;
- recovery from `running`: the same Hermes run ID is waited/finalized and the
  exact body is recovered with no `ensure()` or new run submission;
- recovery after `agent_ready`: Fleet may discover an already-created body by
  exact plan identity, but never creates a replacement;
- recovery from `body_ready`: no Hermes run is silently submitted because the
  original submission status cannot be proven from that durable state alone;
- recovery from `run_submitting`: resubmission is categorically forbidden;
- cleanup is idempotent when the body has already disappeared;
- Agent Instance profile remains present after success, timeout/cancellation,
  failure and recovery paths.

## Real Docker proof

`tests/integration/test_run_capsule_lifecycle_docker.py` creates a real Phase 2
hardened Docker workshop through the Phase 8 Capsule manager while keeping the
Hermes Agent Instance outside the container.

The proof verifies:

- Phase 7 receives the exact live container ID, plan fingerprint and pinned image;
- the workshop is running, `network=none`, and has a read-only root before the
  Hermes run begins;
- Hermes finalization evidence reaches quiescence;
- Fleet removes the exact workshop during Capsule cleanup;
- the same Agent Instance ID/profile remains on disk afterward;
- reopening the SQLite Capsule store yields the same `finalized` Capsule;
- no unfinalized Capsule remains.

After the full suite, Fleet-owned workshop and Phase 4 egress-network residue
checks are empty. Four historical Phase 4 test containers that had never started
were identified by their Fleet labels/age and removed explicitly; no broad
Docker prune was used.

## Current proof

The older Phase 8 implementation remains useful evidence, but the 2026-08-17
re-audit found that the updated master plan had split logical Recipe identity
from ResolvedRecipe identity and added Workflow/compiler provenance binding. It
also found an uncovered crash window where body creation could be indeterminate
before the container ID reached durable Capsule state.

Current master-plan reconciliation proof:

- focused Phase 8 store/executor/live-Docker suite: **26 passed**;
- logical Recipe hash and ResolvedRecipe hash are independently bound and mismatch-tested;
- optional Workflow ID/revision/hash/step binding round-trips through the durable SQLite store and partial binding fails closed;
- compiler/source version plus requirement-provenance digest survive canonical persistence and replay fencing;
- `fleet.run-capsule-spec.v2` is exact and versioned; legacy/incomplete/unknown persisted Capsule shapes fail closed;
- indeterminate body-creation recovery rediscovers and destroys only the exact plan-owned body, with no second `ensure()` and no Hermes submission;
- final full Fleet Python suite: **906 passed, 1 skipped**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS;
- Fleet PR **#143** first exact implementation head `8bd587bd1b417b94b2d2ffca6e62cf7f5e1d8b35`: all five CI jobs PASS in run `32096597660`;
- PR #143 clean-install smoke on that head: Phase 7 installed runtime-seam probe PASS and complete Fleet suite **896 passed, 11 skipped**.

No Hermes Agent code change is required in Phase 8. The executor consumes the
canonically merged Phase 7 native run-scoped contract from
`Dadmin88/hermes-agent:main` merge `04be624ceb3ebadca0d514f4276a146cdc7296e9`.
The closure-status commit intentionally changes PR #143 after the first green
implementation head. Per repository policy, fresh CI must pass on that new exact
head before merge, and the resulting Fleet `main` merge commit must then pass its
push CI. The `COMPLETE` label is not itself closure evidence.

## Explicit later-phase ownership

Phase 8 does not claim completion of:

- Workflow graph → Candidate/Validated/Resolved Recipe compilation, first-run discovery, GPU/resource inference, or adaptive Recipe revision: Phase 8A;
- authenticated principal identity/revocation: Phase 9;
- canonical signed/immutable RunAuthority issuance, replay and narrowing rules:
  Phase 10;
- scoped persistent memory and retrieval policy: Phases 11–12;
- secret interception and Vault-backed temporary handles: Phases 13–14;
- scoped skill learning/quarantine/verification/promotion: Phases 15–18;
- Templar gates: Phases 19–23;
- full audit/provenance chain: Phase 26;
- cross-machine Capsule reconciliation through Nodescale/Keryx: Phase 31;
- migration/removal of the legacy disposable-profile execution path: Phase 37.

Those phases now have a durable Capsule lifecycle to bind to without moving
per-run state into the persistent Hermes profile.
