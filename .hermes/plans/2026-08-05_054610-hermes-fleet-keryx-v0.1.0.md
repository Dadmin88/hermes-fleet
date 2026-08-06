# Hermes Fleet v0.1.0 - Completed Keryx Implementation Record

> Status: complete and deployed. This file is the final implementation record for the v0.1 plan. The earlier `.hermes/plans/2026-08-05_052149-hermes-fleet-v0.1.0.md` remains superseded and must not be executed.

## Goal

Build a lean Hermes plugin and `fleet-node` adapter that let independent Hermes-capable nodes communicate and deliberately delegate execution through Hermes Keryx. Fleet provides friendly identity, exact-node selection, bounded envelopes, policy, dispatch, CLI/tools, and operational views. Keryx remains the sole authenticated transport and durable task/result data plane.

## Final status

The standing goal is complete.

- Fleet runtime acceptance SHA: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`
- Fleet CI: run `31062104463`, passed
- Keryx deployed SHA: `f4ee645e415600a959ea8062d1143140bd6c2616`
- Keryx default-branch integration: [Dadmin88/hermes-keryx#36](https://github.com/Dadmin88/hermes-keryx/pull/36)
- Fleet committed-byte suite: 257 tests passed at runtime acceptance
- Keryx Python SDK: 98 tests passed
- Keryx Rust formatting, Clippy, and workspace tests passed
- Final exact-SHA reviewers found no release blockers
- Both repository worktrees were reported clean and matching their pushed remotes at release time

## Product boundary

Fleet is a communication and coordination layer, not only a remote task runner.

Initial operations:

- `fleet.health`: direct bounded health/capability response, no Hermes run;
- `fleet.inventory`: direct safe node inventory, no Hermes run;
- `fleet.message`: direct bounded text acknowledgment, no Hermes run;
- `fleet.hermes.run`: deliberate executable operation, exactly one bound Hermes run.

Keryx owns peer identity, registration, transport, routing, durable task/result state, claims, leases, route receipts, cancellation records, and offline mailbox behavior. Fleet does not duplicate those systems.

Kanban is not a transport, queue, router, execution engine, or source of truth.

## Completed phases

### Phase 0 - Keryx product truth and baseline

Completed:

- reconciled Keryx source and product documentation;
- verified authenticated registration and task/result paths;
- verified Rust and Python gates;
- identified real limits around cancellation, mailbox durability, and artifacts.

### Phase 1 - Fleet local foundation

Completed:

- standalone Hermes plugin scaffold;
- schema-v1 inventory and configuration;
- friendly name to immutable Keryx peer ID mapping;
- default-deny node policy and bounded request limits;
- strict versioned envelopes;
- deterministic exact-node selection;
- owner-safe local state initialization;
- clean-install Hermes plugin validation.

### Phase 2A - Safe communication and text-result execution seams

Completed:

- authenticated registration lifecycle;
- exact-peer submission;
- actual route/routed-peer receipts;
- absolute deadline transport;
- authenticated sender identity at the worker;
- durable terminal text results;
- public task status/result reattachment by task ID.

No additional Fleet transport or lifecycle database was introduced.

### Phase 3 - fleet-node adapter

Completed:

- one foreground Keryx worker service;
- one explicit direct-versus-executable dispatcher;
- direct health, inventory, and message handlers;
- authenticated loopback Hermes Runs client;
- approval fail-closed behavior;
- shared absolute deadline for health probes;
- worker-level deadline wrapper;
- durable four-state execution binding;
- known-run resume and completed-result replay;
- duplicate execution prevention;
- lazy Keryx imports for plugin/runtime separation.

### Phase 4 - Controller and live inventory

Completed:

- direct public Keryx controller adapter;
- exact configured-node selection;
- actual Keryx route preservation;
- live node projection with distinct direct, registry-visible, not-visible, and unknown states;
- peer-produced response content consistently marked untrusted.

### Phase 5 - Exact-node communication, execution, and results

Completed:

- message, health, inventory, and run submission through one Keryx primitive;
- durable status retrieval by task ID;
- explicit fail-closed cancellation surface;
- no automatic retry after ambiguous submission;
- no duplicate Fleet task/result database.

### Phase 7 - Hermes tools, CLI, and operator skill

Completed:

- seven async model tools;
- bounded `hermes fleet` CLI tree;
- plugin-root `SKILL.md` operator guidance;
- plugin manifest and clean-install registration coverage.

Phase 6 fan-out was intentionally deferred and was not required for v0.1.

### Phase 8 - Deployment

Completed:

- read-only preflight on Katana and the VPS;
- removal of the unsafe bridge from the active path;
- authenticated private relay/registry deployment;
- Keryx daemon and edge-node deployment on both machines;
- VPS `admin` Hermes Runs API on loopback;
- VPS `fleet-node` service;
- rollback snapshots and deployment runbook.

### Phase 9 - Real acceptance

Completed:

#### Slice A

- Task: `7e78f4c1-240a-496f-bbf4-2a0a491018d6`
- Operation: `fleet.message`
- Text: `FLEET_MESSAGE_OK`
- Route: `relay`
- Acknowledgment: `received`
- Hermes runs: `0`
- Binding rows: `0`
- Peer response: `untrusted: true`

#### Slice B

- Keryx task: `913af216-2866-48e8-8f18-b479df479466`
- Hermes run: `run_b9f345d82c3d45778b14714966922f7e`
- Route: `relay`
- Result: `FLEET_OK`
- Binding: `completed`
- Reattached status: `completed`, result `FLEET_OK`

Live health, inventory, list, durable status, duplicate prevention, a one-second health deadline, and final trust-boundary checks also passed.

## Deployed services

Katana active and enabled:

- `keryxd.service`
- `keryx-node.service`

Katana disabled and inactive:

- `keryx-task-bridge.service`
- `keryx-node-refresh.service`

VPS active and enabled:

- `keryx-relay.service`
- `keryxd.service`
- `keryx-node.service`
- `hermes-fleet-api.service`
- `fleet-node.service`

## Final trust and safety corrections

The final exact-byte review found and corrected two release blockers before acceptance:

1. Peer-originated direct responses were incorrectly marked trusted. All direct and executable peer outputs now return `untrusted: true`.
2. `fleet.health` computed the task deadline but initially failed to bound both synchronous HTTP probes with the same remaining budget. The worker now shares one absolute deadline across the probes and applies `asyncio.wait_for` around the blocking thread call.

## Known limitations

- Cross-node cancellation is unavailable and fails closed.
- Relay offline mailbox state is in-memory and does not survive relay restart.
- `asyncio.wait_for` cannot forcibly kill a custom thread implementation that ignores its own timeout; the production Runs client honors and propagates the HTTP deadline.
- `node_service.py` may call `node.stop()` twice during normal shutdown. This is nonblocking cleanup outside the accepted communication/execution paths.
- The deployed Tailscale TLS certificate expires on 2026-09-17 and must be renewed, followed by a relay restart.
- A running Katana Hermes gateway must be restarted after plugin updates before the model tools are loaded in that gateway process.

## Deferred backlog

The following are explicitly outside v0.1:

- cross-node cancellation transport and worker observation;
- tag fan-out and partial-result orchestration;
- cross-node artifact-byte transport and retrieval;
- pub/sub, broadcast, persistent inboxes, and multi-node chat;
- agent-session routing and workflow graphs;
- Kanban integration;
- Android/Termux;
- public-internet exposure;
- multi-tenant architecture.

## Completion rule

No further v0.1 implementation work is required. Future changes should be opened as narrow maintenance or post-v0.1 feature slices and must preserve the proven separation:

```text
Fleet selects and communicates
Keryx authenticates, routes, and records
fleet-node validates and dispatches
Hermes executes only for fleet.hermes.run
```
