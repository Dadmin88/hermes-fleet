# Hermes Fleet v0.1.0 — Keryx Implementation Plan

> **Execution contract:** vertical-slice TDD, one implementation writer at a time, risk-proportionate review, and focused commits only after a functional phase gate passes. Preserve unrelated work.

## Goal

Build a lean Hermes general plugin and `fleet-node` adapter that let independent Hermes-capable nodes communicate and deliberately delegate execution through Hermes Keryx. Fleet provides friendly inventory, operator tags, selection, communication envelopes, policy, dispatch, CLI/tools, and operational views; Keryx remains the sole transport and durable task/result data plane.

## Current status and operating rule

- The local Phase 1 foundation is complete.
- Generalized security hardening is frozen after the current local-contract packet.
- The project is entering the functional Katana-to-VPS vertical-slice phase.
- The first release target is one Katana controller, one VPS worker, one direct `fleet.message` acknowledgment, and one deliberate text-only `fleet.hermes.run` result.
- Artifact transport, capability tags, fan-out, Android/Termux, and richer orchestration are deferred backlog, not current release requirements.

Before starting work, ask: **“Is this required to prove one direct node message and one remote Hermes execution between Katana and the VPS?”** Proceed only when the answer is yes. Otherwise record the item in the backlog and continue toward the two proofs. Only a concrete critical vulnerability may interrupt the milestone.

Report progress as working capability: a message reached the VPS without calling Hermes, the acknowledgment returned with the actual Keryx route, a deliberate execution created one Hermes run, terminal text returned, duplicate execution was prevented, and both two-machine smokes passed. Test totals are supporting evidence, not milestones.

## Authoritative inputs

- Fleet repository: `/home/kyle/Create/repos/hermes-fleet`, branch `main`.
- Keryx repository: `Dadmin88/hermes-keryx`, integration baseline `22d66ecf452eb6c1f87bd3710dda5ec665f5f32c`.
- Keryx Phase 17 implementation: PR #29, merge commit `906823badac04fd9d159c4da927dda5c25d712dc`.
- Hermes source: `/home/kyle/.hermes/hermes-agent`, audited at `aec331899e4748739927fddf02a54327e64419a0`.
- Architecture: `docs/architecture.md`.
- Superseded plan: `.hermes/plans/2026-08-05_052149-hermes-fleet-v0.1.0.md`; do not execute it.

## Non-negotiable scope boundary

Fleet does not build a relay, daemon, transport protocol, task database, offline queue, result poller, artifact protocol, WebSocket controller, SSH executor, dashboard, scheduler, workflow engine, auto-updater, cluster, filesystem, shared memory, certificate authority, automatic discovery system, or self-healing layer.

Direct Hermes A2A is not a v0.1 transport or fallback.

## Keryx verification list for the vertical slice

First verify each seam against the current public Keryx API. Use it directly when it exists. Make a narrow upstream change only for a concrete missing behavior; never emulate Keryx inside Fleet.

Current-proof seams are authenticated peer-owned registry mutation, registration refresh/deregistration, exact-peer submission, absolute remote deadline transport, authenticated sender identity, actual-route/routed-peer receipt, and durable completed/failed terminal text result retrieval. Cross-node running-task cancellation and controller task-handle reattachment remain incomplete and are not blockers for the first two smokes.

Capability-tag propagation, artifact-byte transport/retrieval, and relay-mailbox durability are deferred.

## Phase gates

Every phase follows:

1. Write failing focused tests.
2. Run them and record RED evidence.
3. Add the smallest implementation.
4. Run focused and affected suites.
5. Run focused review proportional to the changed functional seam.
6. Inspect `git status --short`; stage and commit only intentional phase files after approval.

Do not start a dependent phase while its gate is open.

### Test priority for the current milestone

1. Controller-to-Keryx `fleet.message` submission and direct acknowledgment without Hermes.
2. `fleet-node` authenticated receipt, metadata validation, and explicit dispatch.
3. Authenticated Hermes run creation only for `fleet.hermes.run`.
4. Task/run binding and duplicate prevention.
5. Terminal text result propagation.
6. Deadline behavior and truthful cancellation limitations.
7. Both real Katana/VPS smoke tests.

Do not maximize test count. Prefer a smaller deterministic set that proves the real path.

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

- [x] Keryx source and PR/issue history agree on Phase 17 completion.
- [x] Rust workspace passes.
- [x] Python SDK baseline passes: 29 tests on Python 3.11.
- [x] Authenticated two-node task/result/artifact-descriptor E2E passes.
- [x] Product docs contain no stale “Phase 17 pending” claim.
- [x] No secrets or generated test keys are committed.

## Phase 1 — Reconcile and scaffold Fleet domain

**First action:** inspect the untracked output from the superseded A2A writer. Preserve reusable transport-independent work; delete or rewrite A2A-specific assumptions. Do not accept it merely because tests pass.

**Files**

- `plugin.yaml`, `pyproject.toml`, root `__init__.py`
- `hermes_fleet/{models,config,inventory,selection,envelope,policy,formatting}.py`
- `tests/unit/test_{models,config,inventory,selection,envelope,policy}.py`
- `tests/test_plugin_registration.py`

The recovered untracked Phase-1 scaffold is input, not accepted architecture. Preserve its public plugin registration and atomic profile-state helpers where tests prove them; remove URL/token fields, Agent Card models, and the generic `FleetTransport` A2A seam. Fix valid-empty-cache rewrites, non-integer default validation, SPDX license metadata, and duplicate friendly-name/peer-ID handling under TDD.

**Behaviors**

1. Register placeholder `fleet` CLI and toolset through public Hermes plugin APIs.
2. Resolve state under active `HERMES_HOME/fleet/` with standalone-test override.
3. Atomic, owner-safe YAML writes for operator inventory only.
4. Friendly name → immutable Keryx peer ID mapping; operator tags, enabled, priority, and node policy.
5. Versioned `fleet.health`, `fleet.inventory`, `fleet.message`, and `fleet.hermes.run` envelopes.
6. Reject unknown versions/operations, invalid peer IDs, malformed tags, duplicate names, and unsafe limits.
7. No URLs, bearer values, private keys, or duplicated task lifecycle state in Fleet inventory.

**Gate**

- Focused unit/plugin tests pass.
- `ruff check` and `ruff format --check` pass for changed paths.
- Independent review confirms no direct-A2A or duplicate Keryx machinery remains.

## Phase 2A — First safe node communication and text-result execution

Inspect current public Keryx behavior first. Prefer direct use of existing APIs; if a required seam is genuinely absent, make the smallest upstream change. Keep Fleet adapters thin. Artifact and capability-tag work is explicitly excluded from this gate.

**Compatibility result**

Public Keryx already supports authenticated registration ownership, lifecycle refresh/deregistration, exact-peer submission, absolute deadline transport, authenticated sender identity, immutable route receipts, worker claim/complete/fail, and durable completed/failed text results. No Rust/protobuf Keryx patch blocks the first communication or execution smoke.

Supported but incomplete seams are controller task-handle reattachment after restart and cross-node running-task cancellation. Defer both unless a concrete first-release proof requires them; do not create Fleet-owned result polling or cancellation machinery.

**Target vertical slice**

```text
Fleet controller
→ Keryx task submission
→ remote fleet-node
→ explicit direct handler OR authenticated loopback Hermes Runs API
→ acknowledgment OR Hermes terminal text result
→ Keryx durable result
→ Fleet controller retrieval
```

The first real proof uses Katana as controller and the VPS as the remote node. It must prove both `fleet.message` without a Runs API call and `fleet.hermes.run` with exactly one Runs API call. Both carry text only; no artifact transport, export collection, capability tags, or download helper is required.

**Constraints**

- Do not change protocol wire fields unless source proves an existing field is insufficient.
- Regenerate committed Python stubs only when an unavoidable protocol change requires it.
- Preserve existing SDK signatures through optional keyword-only parameters.
- Preserve backward decode compatibility for new deadline fields and fail closed when authenticated ownership cannot be established.
- Do not begin Phase 2B merely to make Phase 2A look complete.

**Gate**

- Full Keryx Rust, Python, and authenticated two-node gates pass after each upstream slice.
- Fleet pins or declares a Keryx version/commit containing all Phase 2A seams.
- One real Katana-to-VPS direct message returns an acknowledgment without invoking Hermes and reports the actual route/routed peer.
- One real Katana-to-VPS Hermes run returns terminal text through Keryx, reports the actual route/routed peer, and cannot execute twice on reclaim.
- Deadline, ownership denial, TTL refresh, and graceful deregistration are observed through public surfaces; cancellation is reported only to the extent Keryx actually supports it.

## Deferred backlog — Phase 2B capability tags and cross-node artifacts

This is not a first-release requirement. Start only after the Katana/VPS text-result release gate passes and the work is separately approved.

**TDD slices**

1. Add optional capability tags to the Python SDK `Skill` model and registration path; prove `SkillInfo.tags` reaches `RegisterSkills`. Fleet operator tags remain local inventory metadata.
2. Add bounded authenticated artifact-byte transport owned by Keryx.
3. Enforce an aggregate cross-node artifact limit distinct from the node-local blob ceiling.
4. Verify descriptor names, sizes, and digests before origin-side content-addressed ingestion.
5. Make ingestion and delivery replay-safe and duplicate-safe.
6. Add high-level Python artifact list/get/download helpers with safe destination handling.

**Required adversarial matrix**

- binary content;
- empty files;
- multiple files;
- traversal names;
- replay;
- duplicate ingestion;
- hash mismatch;
- per-file and aggregate oversize cases.

**Gate**

- Full Keryx Rust, Python, and authenticated two-node gates pass.
- Origin retrieval proves bytes, digest, size, replay, and aggregate limits through public Python APIs.
- Fleet adds artifact-facing controller/node behavior only after the pinned Keryx capability is accepted.

## Phase 3 — `fleet-node` adapter

**Files**

- `hermes_fleet/node/{service,handlers,hermes_runs,bindings,policy}.py`
- `hermes_fleet/node_cli.py`
- `tests/node/test_{health,inventory,hermes_runs,policy,cancellation}.py`
- fake authenticated Hermes Runs API fixture

**Behaviors**

1. Start `KeryxNode`, register the four Fleet operations, refresh registration before TTL expiry, deregister on graceful shutdown, and run its worker loop.
2. Register one dispatcher handler, not one handler per operation; validate authenticated sender identity, parse the sole JSON text part, and cross-check target/version/operation metadata before dispatch.
3. `fleet.health`: return adapter/Keryx/Hermes capability health without a model call.
4. `fleet.inventory`: return bounded safe identity/version/capability data.
5. `fleet.message`: accept bounded text and optional topic/correlation ID, return safe delivery acknowledgment metadata, and never invoke Hermes.
6. `fleet.hermes.run`:
   - authenticated loopback `GET /v1/capabilities`;
   - `POST /v1/runs`;
   - poll `GET /v1/runs/{id}`;
   - stop/fail if Hermes enters `waiting_for_approval`; never call the approval endpoint;
   - persist a crash-safe Keryx-task-ID → Hermes-run-ID binding;
   - on reclaim resume only the bound run; fail `execution_uncertain` for a pre-submit crash window or missing bound run; never auto-resubmit;
   - require empty `input.export_paths` during the text-only Phase 2A proof;
   - map completed/failed/cancelled output to Keryx result metadata and bounded terminal text.
7. Register `fleet.hermes.run` only when required Runs capabilities are available.
8. Enforce default-deny sender peer, `fleet.*` operation, metadata/envelope agreement, deadline, payload size, zero Phase 2A export paths, and local policy before dispatch.
9. Keep API key in environment/config secret scope; redact errors.
10. Use a true foreground `serve_forever()` entry point suitable for systemd supervision.

**Gate**

- Tests prove health/inventory/message never call Hermes; message acknowledgments contain only bounded safe metadata; sender/target/operation metadata mismatches fail closed; and the Runs client covers success, auth failure, malformed response, timeout, approval wait, and secret redaction.
- A no-duplicate-run test proves reclaim never submits a second Hermes run for the same Keryx task ID.
- Binding tests prove atomic first reservation, known-run resume, completed-text replay, and fail-closed handling of uncertain creation without resubmission.
- Real local loopback smoke against one Hermes installation passes before deployment packaging.

After Phase 2B is accepted, extend the adapter with `artifacts.py`, focused artifact tests, private per-task export roots, no-follow post-run collection, and bounded packaging. Those additions are not part of the first text-result gate.

## Phase 4 — Controller Keryx adapter and inventory views

**Files**

- `hermes_fleet/keryx/{client,discovery,status}.py`
- `hermes_fleet/cli.py`
- `tests/keryx/test_{discovery,status,reachability}.py`
- fake Keryx client fixture for Fleet CI

**Behaviors**

1. Wrap only public Keryx Python SDK APIs added/verified in Phase 2; do not reach into generated private stubs from Fleet.
2. Display configured, registry-visible, direct-connected, last actual submission route, unknown/offline, and proven-round-trip states separately. Never infer mailbox eligibility from registry visibility.
3. Merge live Keryx capabilities with Fleet friendly metadata without mutating Keryx records.
4. Implement `init`, node CRUD/tagging, `list`, `show`, and `doctor`.
5. `doctor` checks inventory schema, Keryx daemon health, local peer ID, registry access, direct peers, required skills, and local Hermes Runs capability where applicable.

**Gate**

- Partial/disconnected state is deterministic and never reported as online solely from registry presence.
- No registry or task database duplication.

## Phase 5 — Exact-node communication/execution dispatch and results

**Files**

- `hermes_fleet/dispatch.py`
- `hermes_fleet/results.py`
- `hermes_fleet/cli.py`
- `tests/dispatch/test_{single,result,cancel,ambiguity}.py`

**Behaviors**

1. Resolve one configured friendly name and policy-check health, inventory, message, or run.
2. Submit one versioned Keryx envelope by immutable peer ID with operation/target metadata and absolute deadline; require empty `export_paths` for Phase 2A.
3. Return Keryx task ID immediately or wait through `TaskHandle.wait()`.
4. Implement task status through public Keryx APIs and expose cancellation only with its truthful current limitations.
5. Add public Keryx task-handle reattachment only if required for controller restart/status acceptance; never poll Keryx privately from Fleet.
6. Never retry an ambiguous submission automatically.
7. Store at most bounded operational selection/policy events keyed to Keryx task ID; no duplicate lifecycle database.

**Gate**

- Fake Keryx tests cover direct, mailbox, timeout, ambiguous submit, durable late text result, and cancellation.
- Real two-node Fleet message and Fleet run both pass on Linux before fan-out.

Artifact download and content-retrieval tests are added to this phase only after Phase 2B provides accepted public Keryx APIs.

## Deferred backlog — Phase 6 tag fan-out and partial results

This phase is not required for the first one-controller/one-worker release.

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

1. Register `fleet_list_nodes`, `fleet_get_node`, `fleet_get_health`, `fleet_send_message`, `fleet_run`, `fleet_get_task`, and `fleet_cancel_task`. Register artifact tools and enable nonempty `export_paths` only after Phase 2B.
2. Accept only configured names/tags and Keryx task IDs; never arbitrary URLs or shell commands.
3. Return stable `{success,data,errors,warnings}` JSON.
4. Mark remote output as untrusted data.
5. Register bare skill `fleet-operator` and concise optional `/fleet` command.
6. Verify async model-tool dispatch through the real Hermes plugin loader.

## Phase 8 — Katana/VPS deployment packaging and migration

**Files**

- `deploy/systemd/{keryx-relay,keryxd,fleet-node}.service`
- `docs/{controller-setup,node-setup-linux,security,troubleshooting,smoke-test}.md`

**Work**

1. Inspect and reconcile Katana’s existing Keryx services and SQLite state before changing them.
2. Disable the existing `keryx-task-bridge.service` before activating relay traffic: it reads SQLite directly and fabricates terminal stub completions for pending tasks.
3. Stop the existing restart loop only with explicit operator approval; preserve data/config and avoid a second competing daemon.
4. Deploy one allowlisted relay/registry on the VPS over Tailscale.
5. Deploy one `keryxd` and one foreground `fleet-node` on the VPS worker with private state roots and resource limits.
6. Configure local Hermes API server on loopback with a strong existing secret mechanism.
7. Verify service effective settings, logs, peer IDs, registry TTL refresh, and post-restart behavior.
8. Document that relay mailbox contents do not survive relay restart.

**Safety**

- No `sudo`, service stop/restart, firewall, port, or production config mutation without explicit approval and exact values.
- Never write credentials into repo, chat, fixtures, unit files, or command output.

## Phase 9 — Katana/VPS communication/execution acceptance and first release

Do not declare this phase complete until a repeatable real proof shows:

1. Katana selected the VPS through Fleet inventory.
2. `fleet.message` reached the VPS through Keryx, was handled directly without a Runs API call, and returned an acknowledgment plus actual route.
3. `fleet.hermes.run` reached the VPS and created exactly one authenticated local Hermes run.
4. Hermes returned exactly `FLEET_OK` through Keryx, and reclaim did not create a second run.
5. Logs identify the Keryx task, sender/target peer, operation, actual route, Hermes run when applicable, and final status.
6. GitHub Actions remains green on the exact released commit.
7. Both two-machine smoke procedures are documented and repeatable.

The proof must also exercise the implemented deadline behavior and state cancellation limitations truthfully. Artifacts, fan-out, Android/Termux, pub/sub, inboxes, Kanban integration, and richer orchestration do not block this release.

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
- Real Katana/VPS direct-message and Hermes-execution smoke procedures.
- Leakage search over Fleet/Keryx generated state and captured logs.
- `git diff --check` and scoped `git status --short` in both repositories.

## Primary risks

1. Existing Katana Keryx services target a missing VPS registry and are restart-looping; migrate rather than layering a second stack.
2. The live pre-Phase-17 task bridge fabricates successful completions and must be retired before Fleet traffic.
3. Relay mailbox data is not restart-durable.
4. Cross-node running-task cancellation is incomplete in Keryx; do not claim or emulate it. Absolute deadline transport exists, but already-running handler interruption remains limited.
5. Keryx registry presence is not proof of routability; always prove a task/result round trip.
6. Hermes Runs state has different retention/restart semantics from Keryx durable results; the node adapter must finalize into Keryx before claiming success.

## Deferred backlog

- Capability tags, authenticated artifact-byte transport/retrieval, and export-path collection.
- Tag fan-out, partial-result orchestration, Android/Termux, and broader multi-device acceptance.
- Additional hostile subclasses, exotic object-hook behavior, and obscure Unicode combinations after the current packet.
- Defensive checks inside trusted helpers, generic validation frameworks, and purity-only refactors.
- Hypothetical future multi-tenant attacks and parser cases that cannot traverse the real transport.
- Relay-mailbox restart durability and other noncritical future-scale architecture.
- Pub/sub, broadcast, multi-node chat, persistent inboxes, agent-session routing, priorities, workflow graphs, Kanban integration, public exposure, and multi-tenancy.

Backlog items do not interrupt the vertical slice unless they expose a concrete critical vulnerability.
