# Hermes Fleet vNext Phase 1 reliability baseline

Phase 1 preserves the already-proven autonomy and transport reliability behavior before vNext changes the execution body, persistent Agent lifecycle, authority model, learning system, or cross-machine recovery layers.

This document is an **acceptance record**, not a statement that later vNext architecture has shipped. It maps each Phase 1 requirement to the current owning implementation and its proof.

## Acceptance rule

A historical branch or old soak is evidence only. Phase 1 is accepted only where the required behavior has been reconciled against the current implementation or where the owning upstream repository already contains the proven fix.

The vNext ownership and lifecycle rules in [vNext foundation](vnext-foundation.md) remain authoritative. In particular, preserving a reliability primitive does not preserve the retired disposable-Hermes-profile architecture that happened to exist when some of these fixes were first developed.

## Fleet canonical reconciliation

The Phase 1 Fleet reconciliation is present on canonical Fleet `main`. The working branch `vnext/phase1-reliability-baseline` is historical implementation provenance only and is not a closure authority.

Reconciled commits after Phase 0:

- `03dac1d` — exact-Recipe approval scoping;
- `9a5a60f` — destination-scoped Hermes toolset ceiling and Recipe subset;
- `83d4d04` — Hermes finalization/quiescence barrier before cleanup;
- `87fe96d` — bounded run approval authority and narrowed tool authority;
- `9368bf6` — transport status separated from execution status;
- `6f88474` — semantic Hermes outcome verification foundation;
- `5515199` — command/process outcome evidence and pending-process verification;
- `528780f` — hardened local Ollama runtime support retained from the proven baseline;
- `3358376` — Fleet execution loop warning/hard-stop bounds;
- `379a253` — Fleet execution terminal forced foreground;
- `4e2afaf` — bounded read-only inventory-preflight retry;
- `0cdbbc1` — bounded Fleet model-turn budget;
- `896f796` — pinned Agency Git object cache;
- `6e95af0` — exact-identity Keryx delivery retry;
- `358d1e6` — deadline-expiry classification;
- `a67ae74` — exact-execution typed outcome evidence so transport completion cannot launder an execution failure;
- `efeb1ea` — formatting-only cleanup after semantic conflict resolution.

## Hermes Agent canonical reconciliation

Hermes Agent Phase 1 was reconciled onto the current `Dadmin88/hermes-agent` `main` through PR #1 (`Phase 1: preserve Fleet reliability baseline`). The PR merged only after all required checks were green. Merge commit: `974206859884089f8d194446f8dae4730b6108c2`.

The original `vnext/phase1-reliability-baseline` branch remains historical source evidence only. Its eight Phase 1 commits were replayed onto current fork `main`; hundreds of unrelated upstream commits carried by that historical branch were deliberately excluded.

Canonical replay commits before merge:

- `6a829ddae` — `/v1/runs/{run_id}/finalize` and profile-runtime quiescence barrier;
- `6c157b2c2` — bounded API-run approval authority;
- `e0be21750` — run tool-outcome evidence;
- `a52d14541` — command exit/process evidence;
- `3bf3ca264` — foreground-only terminal profile support;
- `833353824` — multiplex profile iteration-budget enforcement;
- `b928dfcee` — terminal-only `fleet-terminal` toolset;
- `5ff741a5c` — terminology cleanup so finalization no longer assumes Fleet deletes disposable Hermes profiles;
- `88e24713d` — marks `fleet-terminal` as a run/session posture so ordinary user toolset configuration cannot disable the shared core terminal tool.

The later historical Agent branch commits for Fleet-owned Docker sandboxes, principal-private memory, and run scope were deliberately **not** included in Phase 1. They are outside Phase 1 and were not part of PR #1.

## Requirement-by-requirement evidence

### Destination-scoped toolsets

Owner: Fleet + Hermes Agent.

Fleet treats destination toolsets as a ceiling and requires the exact Recipe to request a subset through `fleet.hermes/toolsets.v1`. Invalid, duplicate, overbroad, or disallowed toolsets fail closed before execution. Hermes Agent exposes the `fleet-terminal` toolset used by this path.

Primary Fleet commits: `9a5a60f`, `87fe96d`.

Primary Agent commit: `b928dfcee`.

### `fleet-terminal`

Owner: Hermes Agent.

Canonical Hermes Agent `main` contains the terminal-only Fleet toolset and its API-server toolset tests after PR #1.

Primary Agent commit: `b928dfcee`.

### Foreground terminal coercion

Owner: Fleet policy + Hermes Agent terminal primitive.

Fleet stages `terminal.force_foreground: true` for the Fleet execution context. Agent honors foreground-only terminal profiles without changing interactive Hermes defaults.

Primary Fleet commit: `379a253`.

Primary Agent commit: `3bf3ca264`.

### Bounded model-turn budgets

Owner: Fleet policy + Hermes Agent runtime.

Fleet stages `agent.max_turns: 8` for Fleet execution. Agent honors per-profile iteration budgets in multiplex mode.

Primary Fleet commit: `0cdbbc1`.

Primary Agent commit: `833353824`.

### Approval budgets

Owner: Fleet + Hermes Agent independently.

The exact Recipe can request `fleet.hermes/approvals.v1` only with `mode: once` and a bounded `max_requests`. Fleet validates and forwards the budget. Hermes Agent independently enforces the API-run approval budget.

Primary Fleet commits: `03dac1d`, `87fe96d`.

Primary Agent commit: `6c157b2c2`.

### Hermes + Fleet independent approval enforcement

Owner: both layers.

Fleet rejects unsupported approval modes/budgets before starting a run. Hermes Agent tracks the remaining approval authority for the API run and refuses excess authority independently of Fleet.

### Finalization/quiescence barrier

Owner: Hermes Agent primitive, Fleet lifecycle enforcement.

Hermes Agent exposes idempotent exact-run finalization that requires a terminal run, exact multiplex profile match, no active sibling run for that profile, SessionDB persistence drain/close, and profile logging drain/detach. Fleet refuses execution-state cleanup until the exact run proves `quiescent: true`.

The reconciled Agent implementation uses current upstream SessionDB cache locking and keeps the newer `/steer` endpoint.

Primary Fleet commit: `83d4d04`.

Primary Agent commits: `6a829ddae`, `5ff741a5c`.

### Transport status separate from execution status

Owner: Fleet operator contract.

`OperatorCompletionResult` carries `transport_status` and `execution_status` separately. A completed Keryx task can therefore contain an exact-ID-bound typed Fleet execution outcome of `failed` or `indeterminate` without being reported as successful execution.

Primary Fleet commits: `9368bf6`, `a67ae74`.

### Process/command evidence

Owner: Hermes Agent evidence production, Fleet semantic verification.

Hermes finalization evidence exposes command-call/error counts and command/process state. Fleet validates this evidence before accepting the declared Recipe outcome.

Primary Agent commit: `a52d14541`.

Primary Fleet commit: `5515199`.

### Actual exit-code semantics

Owner: Hermes Agent.

Canonical Hermes Agent `main` records real command outcome/error state and exit/process evidence through the Phase 1 command-evidence reconciliation rather than trusting model text as proof that a terminal command succeeded.

### Background-process evidence

Owner: Hermes Agent.

Finalization evidence distinguishes completed command work from work that remains alive in the background. Fleet consumes the structured command/process evidence rather than inferring completion from natural-language output.

Primary Agent commit: `a52d14541`.

### Pending-process detection

Owner: Hermes Agent + Fleet.

Hermes finalization reports `pending_processes`. A Recipe using `require_no_pending_processes` becomes `indeterminate` rather than falsely successful while work is still outstanding.

Primary Agent commit: `a52d14541`.

Primary Fleet commit: `5515199`.

### Semantic outcome verification

Owner: Fleet.

`fleet.hermes/outcome.v1` can require a minimum number of successful commands, successful last command, and zero pending processes. Invalid evidence is indeterminate; verified command failure is failed; only verified evidence can support success.

The typed `fleet-execution-outcome.v1.json` artifact is bound to the exact execution ID so a transport result cannot be substituted across jobs.

Primary Fleet commits: `6f88474`, `5515199`, `a67ae74`.

### Loop hard-stop

Owner: Fleet execution policy + Hermes Agent native loop guardrails.

Fleet execution enables warnings after repeated exact/same-tool/no-progress failures and hard-stops at the bounded configured threshold. Interactive Hermes defaults are not globally rewritten.

Primary Fleet commit: `3358376`.

### Agency pinned Git cache

Owner: Fleet.

Pinned Agency Git objects are cached so repeated exact-source resolution does not require unnecessary repeated fetch/materialization while retaining immutable source identity.

Primary Fleet commit: `896f796`.

### Keryx same-identity redelivery

Owner: Fleet use of Keryx transport identity.

Retry of uncertain delivery reuses the same exact execution/task identity. Fleet does not manufacture a replacement execution merely because transport delivery is retried.

Primary Fleet commit: `6e95af0`.

### Inventory-preflight retry

Owner: Fleet.

Only the read-only destination inventory probe retries: three bounded attempts with bounded delays and a maximum per-probe deadline. Actual execution submission remains single-shot/idempotency-driven.

Primary Fleet commit: `4e2afaf`.

### Stale control-plane socket recovery

Owner: Hermes Nodescale.

The proven fix is upstream on Nodescale `origin/main`: commit `101190e` (`fix: recover stale operator sockets safely`). It uses a wire-file lock, safe stale-socket recovery, and inode/owner checks. The local Nodescale checkout was intentionally not mutated because another thread owns dirty work there.

### 10-run no-restart reliability proof

Preserved operator soak artifact directory:

`fleet-final-soak-20260815-163659/`

Evidence:

- `proof.txt` records ten separate runs, `FINAL-SOAK-01` through `FINAL-SOAK-10`;
- Fleet commit recorded by the soak: `7099569205ac3399500dc12f0a9d22cbedd04f6f`;
- Hermes Agent commit recorded by the soak: `5ecd55622af5e0d5988d4288d5602a3661f7607b`;
- Katana service snapshot reports `NRestarts=0` for `fleet-managed-projection.service`, `keryxd.service`, and `keryx-node.service`;
- Nitro service snapshot reports `NRestarts=0` for `hermes-fleet-api.service`, `fleet-node.service`, `ollama-fleet.service`, `ollama-fleet-preflight.service`, `keryxd.service`, `keryx-node.service`, and `fleet-demo-web.service`.

The proof remains historical evidence; the behavior it exercised has now been reconciled onto canonical Fleet and Hermes Agent `main` branches rather than relying on the soak's old branch topology.

### Deadline semantics

Owner: Fleet operator contract + Keryx task state.

On execution deadline expiry:

- transport certainty is `indeterminate`;
- execution status is `timed_out`;
- terminal state is `failed`;
- the stable operator error category is `DEADLINE_EXCEEDED`;
- the operator-level completion is returned as a failed terminal execution with timeout classification rather than a transport-success claim.

Primary Fleet commits: `358d1e6` plus the Phase 1 canonical deadline-semantic correction.

### Current hardened baseline and canonical reconciliation

Fleet historical hardening was replayed by behavior rather than merged wholesale. Superseded disposable-profile code was deliberately excluded.

Hermes Agent Phase 1 reliability primitives are now merged into canonical `Dadmin88/hermes-agent` `main` through PR #1. The historical `vnext/phase1-reliability-baseline` branch is source evidence only and is not part of the closure condition.

Nodescale stale-socket recovery remains present on its upstream `origin/main` at commit `101190e`.

## Validation record

### Fleet

Canonical Fleet `main` validation:

- Phase 1-only regression slice: **221 passed** across the exact unit-test files changed by the Phase 1 reconciliation;
- latest post-policy `main` CI run `31988388188`: **success**;
- CI jobs green: Rust workspace compatibility, real Nodescale/readiness proofs, Quality on Python 3.11, Quality on Python 3.13, and Hermes plugin clean-install smoke.

This supersedes the earlier environment note about the external Cargo cache: canonical GitHub CI now provides a successful Rust workspace proof for the current Fleet `main` containing Phase 1.

### Hermes Agent

Canonical Hermes Agent validation after replaying only the eight Phase 1 reliability commits onto current fork `main` and adding the blank-slate compatibility fix:

- local focused Phase 1 + affected blank-slate suite: **102 passed, 2 platform skips**;
- Ruff on the affected Agent/gateway/tool/test surfaces: **passed**;
- `git diff --check`: **passed**;
- PR #1 required checks: **all passed**, including all 12 Python test slices, Windows/macOS tests, Ruff/ty, and supply-chain checks;
- PR #1 merge commit: `974206859884089f8d194446f8dae4730b6108c2`;
- resulting `main` CI run `31991850281`: **success**;
- resulting `main` Docker build/test workflow `31991849650`: **success**.

The initial PR run exposed a real compatibility regression where the new `fleet-terminal` toolset could be persisted as a disabled user toolset and strip the shared core `terminal` tool from Blank Slate. Canonical commit `88e24713d` fixes this by marking `fleet-terminal` as a run/session posture. The unrelated compression-concurrency failure from that same first CI run passed independently on rerun and was not changed by Phase 1.

## Phase 1 closure

Phase 1 is closed only when all of the following are simultaneously true:

1. the Fleet Phase 1 reconciliation is present on canonical Fleet `main` and canonical Fleet CI is green;
2. the Hermes Agent Phase 1 primitives are present on canonical Agent `main`, having merged through a green PR, and the resulting Agent `main` CI is green;
3. Nodescale stale-control-plane socket recovery remains present on its canonical upstream `main`;
4. the preserved ten-run no-restart soak evidence remains available and matches the recorded service snapshots;
5. the Phase 1 reconciliation contains no later-phase Agent runtime/container/principal/learning work.

Historical branches are evidence only and cannot satisfy closure by themselves.

Later phases may replace old execution plumbing, but they must preserve these behavior-level guarantees.
