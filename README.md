# Hermes Fleet

Hermes Fleet is a Keryx-backed communication and coordination layer for Hermes-capable nodes. It gives operators friendly node identity, bounded communication envelopes, exact-node selection, local policy, dispatch, and safe presentation while Keryx remains the authenticated transport and durable task/result ledger.

Remote Hermes execution is one Fleet capability, not Fleet's entire communication model. Receiving a Fleet communication does not automatically start Hermes.

## Initial operations

- `fleet.health` — direct adapter/Keryx/Hermes capability health; no model call.
- `fleet.inventory` — direct safe node identity, version, and capability summary; no broad filesystem inventory.
- `fleet.message` — direct bounded text communication with optional topic and correlation ID; returns a deterministic acknowledgment and does not call Hermes.
- `fleet.hermes.run` — the only initial executable operation; starts one authenticated loopback Hermes run and returns terminal text through Keryx.

All four operations use the same versioned Fleet envelope and the same Keryx submission/result primitives. `fleet-node` has one explicit dispatcher that validates sender, target, envelope, metadata, local policy, limits, and deadline before selecting a direct handler or the Hermes execution handler.

## Responsibility boundary

- **Fleet:** friendly node identity and operator metadata, selection, communication envelopes, local policy, dispatch, CLI/model tools, execution binding, and operator presentation.
- **Keryx:** authenticated peer identity, registration/discovery, routing, delivery, durable task/result state, claims, leases, cancellation records, and offline mailbox behavior.
- **fleet-node:** safe local handling of incoming Fleet operations.
- **Hermes:** local agent execution.

Fleet does not create a second transport, message lifecycle database, result poller, relay, workflow engine, or artifact channel. Its narrow SQLite execution binding stores only `Keryx task ID → Hermes run ID → terminal text` to prevent duplicate Hermes execution after reclaim; it is not a competing task ledger.

Kanban is not a transport, queue, router, execution engine, or source of truth. A future dashboard may visualize Keryx-backed state but must not become another state machine.

## Current implementation state

The repository currently provides:

- schema-v1 operator inventory at `HERMES_HOME/fleet/nodes.yaml`;
- friendly names mapped to immutable Keryx `peer_id` values, with no URLs or credentials in inventory;
- default-deny per-node operation policy and bounded deadlines/payloads;
- strict envelopes for all four initial operations, including bounded `fleet.message` fields;
- deterministic exact-name/tag selection;
- a direct Keryx controller adapter that preserves the actual routed peer and delivery route;
- one `fleet-node` dispatcher with direct health/inventory/message handlers;
- authenticated loopback Hermes Runs start/poll/stop support;
- durable fail-closed task-to-run binding and completed-result replay;
- live Keryx inventory with distinct direct/registry/unknown states;
- durable Keryx status reattachment by task ID;
- seven async Hermes model tools, the bounded `hermes fleet` CLI tree, and an operator skill;
- foreground `fleet-node` and systemd deployment units;
- owner-safe local initialization and `hermes fleet init`.

The real Katana↔VPS dual smoke test has not yet passed. Cross-node cancellation remains explicitly unavailable because the current Keryx control plane cannot prove that the destination worker stopped its bound Hermes run.

## Install as a Hermes plugin

Hermes Fleet is a standalone Git directory plugin. Git must already be authorized for the repository:

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

Restart a running gateway after installation or update:

```bash
hermes gateway restart
```

`init` creates missing state files without overwriting valid operator state. The Git checkout installed by the plugin manager is the supported Hermes plugin artifact; the wheel is for development and integration use.

## First functional release gate

The first release is functional only after both real two-machine slices pass:

1. Katana selects the configured VPS and sends `fleet.message` through Keryx; the VPS handles it directly, makes no Runs API request, and returns an acknowledgment plus the actual Keryx route.
2. Katana sends `fleet.hermes.run`; the VPS creates exactly one authenticated local Hermes run, returns `FLEET_OK` through Keryx, and a reclaim cannot create a duplicate run.

Deployment and acceptance details:

- [`docs/deployment.md`](docs/deployment.md)
- [`docs/smoke-test.md`](docs/smoke-test.md)

## Deferred backlog

Explicitly deferred: pub/sub, broadcast, multi-node chat, persistent inboxes, agent-session routing, priorities, workflow graphs, Kanban integration, fan-out, artifacts, Android/Termux, public-internet exposure, and multi-tenant architecture.

Keryx can route result metadata, artifact descriptors, and bounded text previews, but artifact bytes remain destination-local and no proven high-level cross-node download contract exists. Fleet will not add a parallel artifact channel.
