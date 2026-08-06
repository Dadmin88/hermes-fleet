# Hermes Fleet

Hermes Fleet is a coordination layer for Hermes-capable nodes. It gives operators friendly node identity, bounded communication envelopes, exact-node selection, local policy, dispatch, CLI/model tools, and safe presentation while using Hermes Keryx as the authenticated transport and durable task/result ledger.

Remote Hermes execution is one Fleet capability, not Fleet's entire communication model. Receiving a Fleet communication does not automatically start Hermes.

## Release status

Hermes Fleet v0.1 is implemented, deployed, and accepted on the real Katana-to-VPS topology.

Runtime acceptance was completed against:

- Fleet code SHA: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`
- Fleet CI: run `31062104463`, passed on Python 3.11, Python 3.13, and the Hermes clean-install/full-suite smoke
- Keryx SHA: `f4ee645e415600a959ea8062d1143140bd6c2616`
- Keryx integration PR: [Dadmin88/hermes-keryx#36](https://github.com/Dadmin88/hermes-keryx/pull/36)

Accepted live slices:

- `fleet.message`: task `7e78f4c1-240a-496f-bbf4-2a0a491018d6`, relay route, VPS acknowledgment `received`, zero Hermes runs, and zero execution-binding rows.
- `fleet.hermes.run`: Keryx task `913af216-2866-48e8-8f18-b479df479466`, Hermes run `run_b9f345d82c3d45778b14714966922f7e`, relay route, terminal result `FLEET_OK`, completed durable binding, and successful status reattachment.

Live `fleet.health`, `fleet.inventory`, `fleet.list`, and durable status retrieval also passed. All peer-originated direct responses and remote Hermes output are presented with `untrusted: true`.

## Initial operations

- `fleet.health` - direct adapter/Keryx/Hermes capability health; it may perform bounded health probes but never creates a Hermes run.
- `fleet.inventory` - direct safe node identity, version, and capability summary; no broad filesystem inventory.
- `fleet.message` - direct bounded text communication with optional topic and correlation ID; returns a deterministic acknowledgment and does not call Hermes.
- `fleet.hermes.run` - the only initial executable operation; starts one authenticated loopback Hermes run and returns terminal text through Keryx.

All four operations use the same versioned Fleet envelope and the same Keryx submission/result primitives. `fleet-node` has one explicit dispatcher that validates authenticated sender identity, target, envelope, Keryx metadata, local policy, limits, and the absolute deadline before selecting a direct handler or the Hermes execution handler.

## Responsibility boundary

- **Fleet:** friendly node identity and operator metadata, exact-node selection, communication envelopes, local policy, dispatch, CLI/model tools, execution binding, and operator presentation.
- **Keryx:** authenticated peer identity, registration/discovery, routing, delivery, durable task/result state, claims, leases, result routing, cancellation records, and offline mailbox behavior.
- **fleet-node:** safe local handling of incoming Fleet operations.
- **Hermes:** local agent execution.

Fleet does not create a second transport, message lifecycle database, result poller, relay, workflow engine, or artifact channel. Its narrow SQLite execution binding stores only the Keryx task correlation, binding state, Hermes run ID, and bounded terminal result needed to prevent duplicate Hermes execution after reclaim. It is not a competing task ledger.

Kanban is not a transport, queue, router, execution engine, or source of truth. A future dashboard may visualize Keryx-backed state but must not become another state machine.

## Current implementation

The repository provides:

- schema-v1 operator inventory at `HERMES_HOME/fleet/nodes.yaml`;
- friendly names mapped to immutable Keryx `peer_id` values, with no URLs or credentials in inventory;
- default-deny per-node operation policy and bounded deadlines/payloads;
- strict envelopes for all four initial operations;
- deterministic exact-name selection;
- a direct Keryx controller adapter that preserves the actual routed peer and delivery route;
- one `fleet-node` dispatcher with direct health/inventory/message handlers;
- authenticated loopback Hermes Runs start/poll/stop support;
- durable fail-closed task-to-run binding, known-run resume, and completed-result replay;
- live Keryx inventory with distinct direct, registry-visible, not-visible, and unknown states;
- durable Keryx status reattachment by task ID;
- seven async Hermes model tools, the bounded `hermes fleet` CLI tree, and an operator skill;
- foreground `fleet-node` and systemd deployment units;
- owner-safe local initialization with `hermes fleet init`.

## Install as a Hermes plugin

Hermes Fleet is a standalone Git directory plugin. Git must already be authorized for the repository:

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

Restart a running gateway after installation or update so it loads the new model tools:

```bash
hermes gateway restart
```

`init` creates missing state files without overwriting valid operator state. The Git checkout installed by the plugin manager is the supported Hermes plugin artifact; the wheel is for development and integration use.

## Operator surfaces

CLI commands:

```text
hermes fleet init
hermes fleet list
hermes fleet show NODE
hermes fleet health NODE
hermes fleet inventory NODE
hermes fleet message NODE "TEXT" [--topic TOPIC] [--correlation-id ID]
hermes fleet run NODE "PROMPT"
hermes fleet status TASK_ID
hermes fleet cancel TASK_ID
```

Model tools:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

Cross-node cancellation intentionally fails closed because Keryx cannot yet prove that a remote worker observed cancellation and stopped its bound Hermes run.

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Repeatable smoke test and acceptance record](docs/smoke-test.md)
- [Operator skill](SKILL.md)
- [Completed implementation record](.hermes/plans/2026-08-05_054610-hermes-fleet-keryx-v0.1.0.md)

## Known limits and deferred work

- Cross-node cancellation is unavailable and reported honestly.
- Relay offline mailboxes are bounded in-memory queues and do not survive relay restart.
- Cross-node artifact bytes, fan-out, pub/sub, broadcast, persistent inboxes, multi-node chat, workflow graphs, Kanban integration, Android/Termux, public-internet exposure, and multi-tenant architecture remain deferred.
- The deployed Tailscale TLS certificate expires on 2026-09-17 and must be renewed before that date, followed by a relay restart.
- The Katana Hermes gateway must be restarted after a Fleet plugin update before the model tools are available in that gateway process; CLI operations do not require a long-running Fleet controller daemon.
