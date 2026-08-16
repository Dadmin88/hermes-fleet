# Hermes Fleet vNext Phase 1 reliability baseline

Phase 1 preserves the already-proven autonomy and transport reliability behavior before vNext changes the execution body, persistent Agent lifecycle, authority model, learning system, or cross-machine recovery layers.

This document is an **acceptance record**, not a statement that later vNext architecture has shipped. It maps each Phase 1 requirement to the current owning implementation and its proof.

## Acceptance rule

A historical branch or old soak is evidence only. Phase 1 is accepted only where the required behavior has been reconciled against the current implementation or where the owning upstream repository already contains the proven fix.

The vNext ownership and lifecycle rules in [vNext foundation](vnext-foundation.md) remain authoritative. In particular, preserving a reliability primitive does not preserve the retired disposable-Hermes-profile architecture that happened to exist when some of these fixes were first developed.

## Fleet reconciliation branch

Fleet branch: `vnext/phase1-reliability-baseline`

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

## Hermes Agent reconciliation branch

Hermes Agent branch: `vnext/phase1-reliability-baseline`

The branch is based directly on the then-current `NousResearch/hermes-agent` `origin/main` (`8ad055414`) rather than the machine's dirty/divergent local Agent `main`.

It was pushed to the `Dadmin88/hermes-agent` fork so the reconciliation is durable and reviewable.

Reconciled reliability primitives:

- `a6a31ce25` — `/v1/runs/{run_id}/finalize` and profile-runtime quiescence barrier, merged with current upstream `/steer` and thread-safe SessionDB caching;
- `197db9224` — bounded API-run approval authority;
- `0e241f846` — run tool-outcome evidence;
- `0f28430b6` — command exit/process evidence;
- `3fff61647` — foreground-only terminal profile support;
- `56fab27b1` — multiplex profile iteration-budget enforcement;
- `1d89a057f` — terminal-only `fleet-terminal` toolset;
- `5a747591c` — terminology cleanup so finalization no longer assumes Fleet deletes disposable Hermes profiles.

The later historical Agent branch commits for Fleet-owned Docker sandboxes, principal-private memory, and run scope were deliberately **not** included in Phase 1. Those belong to later vNext phases and must be reconsidered under the frozen architecture rather than inherited accidentally.

## Requirement-by-requirement evidence

### Destination-scoped toolsets

Owner: Fleet + Hermes Agent.

Fleet treats destination toolsets as a ceiling and requires the exact Recipe to request a subset through `fleet.hermes/toolsets.v1`. Invalid, duplicate, overbroad, or disallowed toolsets fail closed before execution. Hermes Agent exposes the `fleet-terminal` toolset used by this path.

Primary Fleet commits: `9a5a60f`, `87fe96d`.

Primary Agent commit: `1d89a057f`.

### `fleet-terminal`

Owner: Hermes Agent.

The Agent reconciliation branch contains the terminal-only Fleet toolset and its API-server toolset tests.

Primary Agent commit: `1d89a057f`.

### Foreground terminal coercion

Owner: Fleet policy + Hermes Agent terminal primitive.

Fleet stages `terminal.force_foreground: true` for the Fleet execution context. Agent honors foreground-only terminal profiles without changing interactive Hermes defaults.

Primary Fleet commit: `379a253`.

Primary Agent commit: `3fff61647`.

### Bounded model-turn budgets

Owner: Fleet policy + Hermes Agent runtime.

Fleet stages `agent.max_turns: 8` for Fleet execution. Agent honors per-profile iteration budgets in multiplex mode.

Primary Fleet commit: `0cdbbc1`.

Primary Agent commit: `56fab27b1`.

### Approval budgets

Owner: Fleet + Hermes Agent independently.

The exact Recipe can request `fleet.hermes/approvals.v1` only with `mode: once` and a bounded `max_requests`. Fleet validates and forwards the budget. Hermes Agent independently enforces the API-run approval budget.

Primary Fleet commits: `03dac1d`, `87fe96d`.

Primary Agent commit: `197db9224`.

### Hermes + Fleet independent approval enforcement

Owner: both layers.

Fleet rejects unsupported approval modes/budgets before starting a run. Hermes Agent tracks the remaining approval authority for the API run and refuses excess authority independently of Fleet.

### Finalization/quiescence barrier

Owner: Hermes Agent primitive, Fleet lifecycle enforcement.

Hermes Agent exposes idempotent exact-run finalization that requires a terminal run, exact multiplex profile match, no active sibling run for that profile, SessionDB persistence drain/close, and profile logging drain/detach. Fleet refuses execution-state cleanup until the exact run proves `quiescent: true`.

The reconciled Agent implementation uses current upstream SessionDB cache locking and keeps the newer `/steer` endpoint.

Primary Fleet commit: `83d4d04`.

Primary Agent commits: `a6a31ce25`, `5a747591c`.

### Transport status separate from execution status

Owner: Fleet operator contract.

`OperatorCompletionResult` carries `transport_status` and `execution_status` separately. A completed Keryx task can therefore contain an exact-ID-bound typed Fleet execution outcome of `failed` or `indeterminate` without being reported as successful execution.

Primary Fleet commits: `9368bf6`, `a67ae74`.

### Process/command evidence

Owner: Hermes Agent evidence production, Fleet semantic verification.

Hermes finalization evidence exposes command-call/error counts and command/process state. Fleet validates this evidence before accepting the declared Recipe outcome.

Primary Agent commit: `0f28430b6`.

Primary Fleet commit: `5515199`.

### Actual exit-code semantics

Owner: Hermes Agent.

The Agent hardening records real command outcome/error state rather than trusting model text as proof that a terminal command succeeded. The current upstream base also already contains `8ad055414` (`fix(terminal): warn when exit_code 0 masks a piped build/test failure`), which is preserved under the reconciliation branch.

### Background-process evidence

Owner: Hermes Agent.

Finalization evidence distinguishes completed command work from work that remains alive in the background. Fleet consumes the structured command/process evidence rather than inferring completion from natural-language output.

Primary Agent commit: `0f28430b6`.

### Pending-process detection

Owner: Hermes Agent + Fleet.

Hermes finalization reports `pending_processes`. A Recipe using `require_no_pending_processes` becomes `indeterminate` rather than falsely successful while work is still outstanding.

Primary Agent commit: `0f28430b6`.

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
- Katana service snapshot reports `NRestarts=0` for `hermes-api`, `hermes-fleet-orchestrator`, and `keryx-daemon`;
- Nitro service snapshot reports `NRestarts=0` for `hermes-api`, `hermes-fleet-node`, and `keryx-daemon`.

The proof remains historical evidence; the behavior it exercised has now been reconciled onto current Fleet and Agent branches rather than relying on the soak's old branch topology.

### Deadline semantics

Owner: Fleet operator contract + Keryx task state.

On execution deadline expiry:

- transport certainty is `indeterminate`;
- execution status is `timed_out`;
- the observed underlying Keryx task may already be terminal `failed` when result retrieval becomes unavailable after deadline;
- the stable operator error category is `DEADLINE_EXCEEDED`;
- the operator-level completion is returned as a timed-out execution rather than a transport-success claim.

Primary Fleet commit: `358d1e6`.

### Current hardened baseline and upstream reconciliation

Fleet historical hardening was replayed by behavior rather than merged wholesale. Superseded disposable-profile code was deliberately excluded.

Hermes Agent reliability primitives were replayed onto current `origin/main` and pushed to the `Dadmin88/hermes-agent` fork as `vnext/phase1-reliability-baseline`.

Nodescale stale-socket recovery is already present on its upstream `origin/main`.

## Validation record

### Fleet

Current Phase 1 branch validation after reconciliation:

- Python suite: **747 passed**;
- Ruff lint: **passed**;
- `git diff --check`: **passed**;
- public-hygiene scan: **passed**.

A fresh Rust workspace test rerun was attempted. Cargo is intentionally configured to place build targets on `/media/kyle/External SSD/DevCache`. The external SSD currently returns `Input/output error (os error 5)` even when Cargo tries to create a brand-new approved target directory. The rerun therefore cannot begin compilation.

This is recorded as an environment verification note rather than a Phase 1 code failure because:

1. the full Rust workspace passed immediately before the Phase 1 Python/ops reconciliation;
2. Phase 1 changes no Rust source files;
3. the failure occurs while creating/opening the Cargo target directory, before Rust compilation or tests begin;
4. the deliberate external-build guard was not bypassed to manufacture a green result on the internal disk.

### Hermes Agent

After replaying the reliability slice onto current upstream and installing the repository-declared `dev` + `messaging` dependency groups with `uv sync`:

- focused Phase 1 suite: **98 passed, 2 skipped**;
- Ruff on the affected Agent/gateway/tool/test surfaces: **passed**;
- `git diff --check origin/main...HEAD`: **passed**.

The focused suite covers API runs/finalization/approval/evidence, profile logging release, foreground terminal behavior, Fleet toolsets, and runtime iteration-budget authority.

## Phase 1 closure

Phase 1 is closed when this document, the Fleet reconciliation branch, the durable Agent reconciliation branch, the upstream Nodescale socket fix, and the preserved soak evidence remain available together.

Later phases may replace old execution plumbing, but they must preserve these behavior-level guarantees.
