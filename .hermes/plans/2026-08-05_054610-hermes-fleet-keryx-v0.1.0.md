# Hermes Fleet v0.1.0 — Keryx Implementation Plan

> **Execution contract:** strict TDD, one implementation writer at a time, independent spec/quality review after every phase, focused commits only after the phase gate passes. Preserve unrelated work.

## Goal

Build a lean Hermes general plugin and `fleet-node` adapter that operate independent Hermes installations through Hermes Keryx. Fleet provides friendly inventory, operator tags, selection, policy, Hermes task envelopes, CLI/tools, and operational views; Keryx remains the sole transport and durable task/result data plane.

## Authoritative inputs

- Fleet repository: `/home/kyle/Create/repos/hermes-fleet`, branch `main`.
- Keryx repository: `DeployFaith/hermes-keryx`, audited `main` at `97fcb5d`.
- Keryx Phase 17 implementation: PR #29, merge commit `906823badac04fd9d159c4da927dda5c25d712dc`.
- Hermes source: `/home/kyle/.hermes/hermes-agent`, validated clean `main` at `a991dfc25`.
- Architecture: `docs/architecture.md`.
- Superseded plan: `.hermes/plans/2026-08-05_052149-hermes-fleet-v0.1.0.md`; do not execute it.

## Non-negotiable scope boundary

Fleet does not build a relay, daemon, transport protocol, task database, offline queue, result poller, artifact protocol, WebSocket controller, SSH executor, dashboard, scheduler, workflow engine, auto-updater, cluster, filesystem, shared memory, certificate authority, automatic discovery system, or self-healing layer.

Direct Hermes A2A is not a v0.1 transport or fallback.

## Known Keryx integration gaps

Resolve these upstream with narrow slices or explicitly constrain v0.1; never emulate them in Fleet:

1. Python SDK skill registration does not propagate registry tags.
2. Python high-level `send_task()` does not expose `TaskEnvelope.deadline_ms`.
3. Python worker handlers do not receive a cooperative cancellation signal, so `TaskHandle.cancel()` does not by itself stop local Hermes work.
4. Daemon artifact CRUD is destination-local; Phase 17 returns descriptors/bounded text previews, not artifact bytes, and the high-level Python SDK lacks artifact retrieval wrappers.
5. Relay offline mailboxes are in-memory and do not survive relay restart.

## Phase gates

Every phase follows:

1. Write failing focused tests.
2. Run them and record RED evidence.
3. Add the smallest implementation.
4. Run focused and affected suites.
5. Run independent spec review.
6. Run independent quality/security review.
7. Inspect `git status --short`; stage and commit only intentional phase files after approval.

Do not start a dependent phase while its gate is open.

---

## Phase 0 — Keryx product truth and baseline

**Repositories**

- `/home/kyle/Create/repos/hermes-keryx`
- `/home/kyle/Create/repos/hermes-fleet`

**Work**

1. Reconcile README, current-product, SDK, and Phase 17 docs with source, issue #10, and PR #29.
2. Remove accidental repeated key-file writes from `scripts/e2e_two_node.py`.
3. Verify schema v7 statements against store migrations/constants.
4. Run Keryx gates on a disk-backed Rust host:
   - `cargo fmt --check`
   - `cargo clippy --workspace --all-targets -- -D warnings`
   - `cargo test --workspace`
   - Python SDK tests
   - workspace binary build
   - authenticated `scripts/e2e_two_node.py`
5. Record real limits: in-memory mailbox, SDK tag/deadline/cancellation gaps.

**Acceptance**

- [ ] Keryx source and PR/issue history agree on Phase 17 completion.
- [ ] Rust workspace passes.
- [x] Python SDK baseline passes: 29 tests on Python 3.11.
- [ ] Authenticated two-node task/result/artifact E2E passes.
- [ ] Product docs contain no stale “Phase 17 pending” claim.
- [ ] No secrets or generated test keys are committed.

## Phase 1 — Reconcile and scaffold Fleet domain

**First action:** inspect the untracked output from the superseded A2A writer. Preserve reusable transport-independent work; delete or rewrite A2A-specific assumptions. Do not accept it merely because tests pass.

**Files**

- `plugin.yaml`, `pyproject.toml`, root `__init__.py`
- `hermes_fleet/{models,config,inventory,selection,envelope,policy,formatting}.py`
- `tests/unit/test_{models,config,inventory,selection,envelope,policy}.py`
- `tests/test_plugin_registration.py`

**Behaviors**

1. Register placeholder `fleet` CLI and toolset through public Hermes plugin APIs.
2. Resolve state under active `HERMES_HOME/fleet/` with standalone-test override.
3. Atomic, owner-safe YAML writes for operator inventory only.
4. Friendly name → immutable Keryx peer ID mapping; operator tags, enabled, priority, and node policy.
5. Versioned `fleet.health`, `fleet.inventory`, and `fleet.hermes.run` envelopes.
6. Reject unknown versions/operations, invalid peer IDs, malformed tags, duplicate names, and unsafe limits.
7. No URLs, bearer values, private keys, or duplicated task lifecycle state in Fleet inventory.

**Gate**

- Focused unit/plugin tests pass.
- `ruff check` and `ruff format --check` pass for changed paths.
- Independent review confirms no direct-A2A or duplicate Keryx machinery remains.

## Phase 2 — Close minimal Keryx SDK seams

Prefer changes in `DeployFaith/hermes-keryx`; keep Fleet adapters thin.

**TDD slices**

1. Add optional tags to the Python SDK `Skill` model and registration path; prove `SkillInfo.tags` reaches `RegisterSkills`.
2. Add optional `deadline_ms`/deadline input to high-level `KeryxNode.send_task()` and `DaemonClient.send_task()`; prove the `TaskEnvelope` carries it and expired work is not claimed.
3. Add a cooperative cancellation observation API for claimed worker tasks, or a cancellation callback/event that a handler can await.
4. Prove a Fleet-style handler can stop a fake local run and reach a Keryx terminal cancellation without completing afterward.
5. Add a Keryx-owned cross-node artifact-content/reference route and safe Python list/get/download wrappers, preserving identifiers, size limits, authentication, and traversal protections. Protocol changes are justified only because the current descriptor-only result is insufficient for Fleet's required artifact retrieval.
6. Preserve `SendTaskResponse.delivery_route` and `routed_to` in a public submission receipt/`TaskHandle` surface so Fleet can report the actual route without guessing mailbox eligibility.

**Constraints**

- Do not change protocol wire fields unless source proves an existing field is insufficient.
- Regenerate committed Python stubs only if proto changes are unavoidable.
- Preserve existing SDK signatures through optional keyword-only parameters.

**Gate**

- Full Keryx Rust/Python/two-node gates pass after each upstream slice.
- Fleet pins or declares a Keryx version/commit containing required seams.

## Phase 3 — `fleet-node` adapter

**Files**

- `hermes_fleet/node/{service,handlers,hermes_runs,policy,artifacts}.py`
- `hermes_fleet/node_cli.py`
- `tests/node/test_{health,inventory,hermes_runs,policy,cancellation,artifacts}.py`
- fake authenticated Hermes Runs API fixture

**Behaviors**

1. Start `KeryxNode`, register the three Fleet skills, and run its worker loop.
2. Register one dispatcher handler, not one handler per skill; validate `target_skill_id`, parse the sole JSON text part, and cross-check operation metadata before dispatch.
3. `fleet.health`: return adapter/Keryx/Hermes capability health without a model call.
4. `fleet.inventory`: return bounded safe identity/version/capability data.
5. `fleet.hermes.run`:
   - authenticated loopback `GET /v1/capabilities`;
   - `POST /v1/runs`;
   - poll `GET /v1/runs/{id}`;
   - stop through `/v1/runs/{id}/stop` on cooperative Keryx cancellation;
   - stop/fail as `approval_required` if Hermes enters `waiting_approval`; never call the approval endpoint;
   - map completed/failed/cancelled output to Keryx result metadata/artifacts.
6. Register `fleet.hermes.run` only when required Runs capabilities are available.
7. Enforce sender peer, operation, deadline, payload size, max-parallel, and local policy before Hermes invocation.
8. Keep API key in environment/config secret scope; redact errors.
9. Use a true foreground `serve_forever()` entry point suitable for systemd/Termux supervision.

**Gate**

- Fake API tests cover success, auth failure, malformed response, timeout, approval wait, cancellation, adapter restart, and secret redaction.
- Real local loopback smoke against one Hermes installation passes before deployment packaging.

## Phase 4 — Controller Keryx adapter and inventory views

**Files**

- `hermes_fleet/keryx/{client,discovery,status}.py`
- `hermes_fleet/cli.py`
- `tests/keryx/test_{discovery,status,reachability}.py`
- fake Keryx client fixture for Fleet CI

**Behaviors**

1. Wrap only public `KeryxNode`/`TaskHandle` APIs.
2. Display configured, registry-visible, direct-connected, last actual submission route, unknown/offline, and proven-round-trip states separately. Never infer mailbox eligibility from registry visibility.
3. Merge live Keryx capabilities with Fleet friendly metadata without mutating Keryx records.
4. Implement `init`, node CRUD/tagging, `list`, `show`, and `doctor`.
5. `doctor` checks inventory schema, Keryx daemon health, local peer ID, registry access, direct peers, required skills, and local Hermes Runs capability where applicable.

**Gate**

- Partial/disconnected state is deterministic and never reported as online solely from registry presence.
- No registry or task database duplication.

## Phase 5 — Single-node dispatch, results, cancellation, artifacts

**Files**

- `hermes_fleet/dispatch.py`
- `hermes_fleet/results.py`
- `hermes_fleet/cli.py`
- `tests/dispatch/test_{single,result,cancel,artifact,ambiguity}.py`

**Behaviors**

1. Resolve one configured friendly name and policy-check `fleet.hermes.run`.
2. Submit a versioned Keryx envelope by immutable peer ID with skill/capability metadata and deadline.
3. Return Keryx task ID immediately or wait through `TaskHandle.wait()`.
4. Implement task status, cancel, and artifact download using Keryx APIs.
5. Preserve durable result retrieval after the controller stops waiting/restarts.
6. Never retry an ambiguous submission automatically.
7. Store at most bounded operational selection/policy events keyed to Keryx task ID; no duplicate lifecycle database.

**Gate**

- Fake Keryx tests cover direct, mailbox, timeout, ambiguous submit, durable late result, cancellation, and artifact descriptors/content retrieval.
- Real two-node Fleet run passes on Linux before fan-out.

## Phase 6 — Tag fan-out and partial results

**Files**

- `hermes_fleet/selection.py`
- `hermes_fleet/dispatch.py`
- `tests/dispatch/test_parallel.py`

**Behaviors**

1. Select all configured nodes matching all requested operator tags.
2. Filter disabled/policy-ineligible nodes before submission.
3. Sort direct-connected, registry-visible, unknown/offline; then priority descending and name ascending. Record actual direct/relay/mailbox route only after Keryx returns it.
4. Bound concurrent submissions/waits with a semaphore.
5. Preserve success beside offline, denied, failed, cancelled, and timed-out nodes.
6. Stable JSON output and nonzero CLI exit when any selected target fails.

**Gate**

- Deterministic tests for ordering, max concurrency, partial success, and empty selection.
- Real workstation + VPS tag fan-out passes.

## Phase 7 — Hermes model tools and operator skill

**Files**

- `hermes_fleet/tools.py`
- root `__init__.py`
- `skills/fleet-operator/SKILL.md`
- `tests/test_tools.py`

**Behaviors**

1. Register `fleet_list_nodes`, `fleet_get_node`, `fleet_run`, `fleet_get_task`, `fleet_cancel_task`, and `fleet_get_artifacts`.
2. Accept only configured names/tags and Keryx task IDs; never arbitrary URLs or shell commands.
3. Return stable `{success,data,errors,warnings}` JSON.
4. Mark remote output as untrusted data.
5. Register bare skill `fleet-operator` and concise optional `/fleet` command.
6. Verify async model-tool dispatch through the real Hermes plugin loader.

## Phase 8 — Deployment packaging and migration

**Files**

- `deploy/systemd/{keryx-relay,keryxd,fleet-node}.service`
- `deploy/termux/*`
- `docs/{controller-setup,node-setup-linux,node-setup-termux,security,troubleshooting,demo}.md`

**Work**

1. Inspect and reconcile Katana’s existing Keryx services and SQLite state before changing them.
2. Disable the existing `keryx-task-bridge.service` before activating relay traffic: it reads SQLite directly and fabricates terminal stub completions for pending tasks.
3. Stop the existing restart loop only with explicit operator approval; preserve data/config and avoid a second competing daemon.
4. Deploy one allowlisted relay/registry on the VPS over Tailscale.
5. Deploy one `keryxd` and one foreground `fleet-node` per device with private state roots and resource limits.
6. Configure local Hermes API server on loopback with a strong existing secret mechanism.
7. Verify service effective settings, logs, peer IDs, registry TTL refresh, and post-restart behavior.
8. Document that relay mailbox contents do not survive relay restart.

**Safety**

- No `sudo`, service stop/restart, firewall, port, or production config mutation without explicit approval and exact values.
- Never write credentials into repo, chat, fixtures, unit files, or command output.

## Phase 9 — Real three-device acceptance and release

Required physical nodes:

- controller/workstation;
- always-on Linux/VPS node;
- Android/Termux node.

**Acceptance matrix**

1. Three distinct stable Keryx peer IDs map to three friendly names.
2. All nodes advertise `fleet.health` and `fleet.inventory`; eligible nodes advertise `fleet.hermes.run`.
3. `hermes fleet list --refresh` distinguishes registry/direct/mailbox states accurately.
4. One named-node Hermes task completes through Keryx.
5. One tag-selected task returns independent results from all matching nodes.
6. Controller stops waiting and later retrieves the durable result by Keryx task ID.
7. At least one artifact is returned and retrieved.
8. A disallowed operation is denied before Hermes invocation.
9. A deadline expires as designed.
10. Offline-node mailbox delivery works across reconnect while relay remains up.
11. Cancellation stops local Hermes through the cooperative path and produces one terminal Keryx state.
12. Restart relay/node/controller checks are reported honestly; no claim of mailbox persistence across relay restart.
13. Secret leakage scan is clean.

No release claim is allowed if Android/Termux is unavailable; report the gate as blocked instead.

## Full validation bundle

```bash
python -m pytest -q
ruff check hermes_fleet tests
ruff format --check hermes_fleet tests
python -m build
```

Also run:

- Keryx full Rust/Python/two-node E2E for any Keryx change.
- Clean temporary `HERMES_HOME` plugin install and registration check.
- Real local Hermes Runs smoke.
- Real workstation/VPS/Android acceptance matrix.
- Leakage search over Fleet/Keryx generated state and captured logs.
- `git diff --check` and scoped `git status --short` in both repositories.

## Primary risks

1. Existing Katana Keryx services target a missing VPS registry and are restart-looping; migrate rather than layering a second stack.
2. The live pre-Phase-17 task bridge fabricates successful completions and must be retired before Fleet traffic.
3. Relay mailbox data is not restart-durable.
4. Cooperative cancellation and Python deadline/tag/artifact propagation require narrow Keryx SDK work.
5. Android background restrictions may interrupt Hermes, `fleet-node`, or `keryxd`; release acceptance must use a real device and documented Termux lifecycle.
6. Keryx registry presence is not proof of routability; always prove a task/result round trip.
7. Hermes Runs state has different retention/restart semantics from Keryx durable results; the node adapter must finalize into Keryx before claiming success.
8. Remote outputs and artifact names are untrusted; sanitize display and extraction paths.
