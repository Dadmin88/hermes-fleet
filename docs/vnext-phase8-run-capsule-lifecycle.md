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

`RunCapsuleSpec` is immutable and content-hashed. It contains:

- execution/run identity and idempotency digest;
- persistent Agent Instance ID;
- principal ID reference;
- resolved Recipe hash;
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
- a missing exact body is never silently replaced;
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

Phase 8 closure evidence:

- focused Phase 8 store/executor/live-Docker suite: **20 passed**;
- Phase 2–8 composition slice before the final crash-fence refinements:
  **143 passed**; the subsequent full suite supersedes it;
- final full Fleet Python suite: **850 passed**;
- full Ruff: PASS;
- `git diff --check`: PASS at the pre-documentation gate and re-run at closure;
- public-hygiene scan: PASS at the pre-documentation gate and re-run at closure;
- Fleet workshop Docker residue: none;
- Fleet Phase 4 egress-network residue: none.

No Hermes Agent code change is required in Phase 8. The executor consumes the
Phase 7 Hermes native run-scoped contract already preserved on the Agent branch
`vnext/phase7-run-scoped-overrides` at `0f2fae340`.

## Explicit later-phase ownership

Phase 8 does not claim completion of:

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
