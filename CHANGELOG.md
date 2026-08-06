# Changelog

## 0.1.0 - 2026-08-05

Hermes Fleet v0.1 is implemented, deployed, and accepted on the real Katana-to-VPS topology.

### Added

- Keryx-backed Hermes node communication and coordination with exact friendly-name selection.
- Strict schema-v1 envelopes for `fleet.health`, `fleet.inventory`, `fleet.message`, and `fleet.hermes.run`.
- One explicit `fleet-node` dispatcher separating direct communication from deliberate Hermes execution.
- Direct health, inventory, and bounded text-message handlers that never create a Hermes run.
- Authenticated loopback Hermes Runs start, poll, stop, approval-fail-closed, and bounded terminal-result handling.
- Narrow durable task-to-run binding with atomic reservation, known-run resume, completed-result replay, and indeterminate fail-closed behavior.
- Public Keryx controller integration preserving actual `task_id`, `routed_to`, and `delivery_route` values.
- Durable task status/result reattachment by Keryx task ID.
- Truthful live node projection distinguishing direct, registry-visible, not-visible, and unknown states.
- Seven async Hermes model tools:
  - `fleet_list_nodes`
  - `fleet_get_node`
  - `fleet_get_health`
  - `fleet_send_message`
  - `fleet_run`
  - `fleet_get_task`
  - `fleet_cancel_task`
- Bounded `hermes fleet` CLI commands for initialization, listing, health, inventory, messaging, execution, status, and fail-closed cancellation.
- Plugin-root operator skill, systemd units, deployment documentation, smoke-test documentation, and owner-safe local initialization.

### Security and correctness

- Default-deny sender and operation policy.
- Authenticated sender identity is taken from Keryx delivery context and cross-checked against envelope and canonical metadata.
- All peer-originated health, inventory, message, and Hermes responses are presented with `untrusted: true`.
- `fleet.health` shares one absolute Fleet deadline across both Hermes HTTP probes and uses worker-level `asyncio.wait_for` to prevent post-deadline completion.
- Duplicate JSON members, non-standard numeric values, malformed Unicode, unknown operations/versions, target mismatches, expired requests, oversized payloads, and metadata disagreement fail closed.
- Remote approval is never auto-granted.
- Cross-node cancellation remains unavailable and returns an explicit error rather than claiming the remote worker stopped Hermes.
- Fleet does not create a second transport, task/message lifecycle database, result poller, artifact channel, workflow engine, or Kanban state machine.

### Live acceptance

Accepted Fleet runtime SHA: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`.

Accepted Keryx SHA: `f4ee645e415600a959ea8062d1143140bd6c2616`, tracked for default-branch integration in [Dadmin88/hermes-keryx#36](https://github.com/Dadmin88/hermes-keryx/pull/36).

- Direct communication task `7e78f4c1-240a-496f-bbf4-2a0a491018d6` returned `received` over the relay with zero Hermes runs and zero binding rows.
- Deliberate execution task `913af216-2866-48e8-8f18-b479df479466` created Hermes run `run_b9f345d82c3d45778b14714966922f7e`, returned exactly `FLEET_OK`, persisted a completed binding, and reopened durable completed status.
- Live health, inventory, list, durable status retrieval, one-second health deadline, duplicate prevention, and trust-boundary checks passed.
- Fleet CI run `31062104463` passed Python 3.11, Python 3.13, package build, Ruff, formatting, and the clean-install/full-suite Hermes plugin smoke.
- Final exact-SHA reviewers found no release blockers.

### Deployment

- Katana runs `keryxd.service` and `keryx-node.service`.
- The historical `keryx-task-bridge.service` and `keryx-node-refresh.service` are disabled and inactive.
- The VPS runs `keryx-relay.service`, `keryxd.service`, `keryx-node.service`, `hermes-fleet-api.service`, and `fleet-node.service`.
- The VPS Hermes Runs API uses the `admin` profile and binds to loopback.
- Rollback snapshots are recorded in `docs/deployment.md`.

### Known limits

- Cross-node cancellation is intentionally unavailable.
- Relay offline mailbox contents do not survive relay restart.
- Cross-node artifact bytes, fan-out, pub/sub, persistent inboxes, Kanban integration, Android/Termux, public exposure, and multi-tenancy remain deferred.
- The deployed Tailscale TLS certificate expires on 2026-09-17 and requires renewal plus relay restart.
- `node_service.py` may call `node.stop()` twice during normal shutdown; this is nonblocking cleanup outside the accepted runtime paths.
