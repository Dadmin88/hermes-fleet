# Hermes Fleet Architecture

## Overview

Hermes Fleet is a communication and management layer for Hermes-capable nodes. It uses Hermes Keryx as the authenticated transport and durable task/result data plane, while Fleet adds operator-friendly node identity, explicit operations, local authorization policy, request validation, dispatch, and safe presentation.

Fleet deliberately distinguishes ordinary communication from AI execution. Health checks, inventory requests, and messages use direct handlers. Only `fleet.hermes.run` may create a Hermes run.

## Responsibility boundary

### Hermes Agent

Hermes owns local execution concerns:

- models and providers;
- tools and skills;
- sessions and memory;
- files and workspaces;
- credentials and approvals;
- local agent runs.

### Hermes Keryx

Keryx owns transport and task lifecycle concerns:

- cryptographic peer identity;
- registration and discovery;
- direct and relay routing;
- durable task and result state;
- claims, leases, heartbeats, and deadlines;
- terminal outcomes and transport receipts;
- cancellation records;
- transport-level mailbox behavior where supported.

### Hermes Fleet

Fleet owns the operator-facing coordination layer:

- friendly node names and tags;
- mapping friendly names to immutable Keryx peer IDs;
- exact-node selection;
- versioned communication envelopes;
- operation and size limits;
- direct-versus-executable dispatch;
- local execution binding;
- CLI and Hermes model tools;
- live operational presentation.

### Private networking

A private network such as Tailscale should limit which machines can reach Keryx and Fleet services. Private reachability is not a substitute for Keryx authentication or Fleet authorization.

## Non-goals

The current Fleet release does not implement:

- another relay or transport protocol;
- a duplicate Keryx task/result database;
- a general offline queue;
- an SSH executor;
- a workflow engine;
- a scheduler;
- an artifact transport;
- a Kanban state machine;
- public-internet hardening.

Kanban or dashboard integrations may project Fleet and Keryx state later, but they must not become a competing execution source of truth.

## Runtime topology

A typical two-node deployment looks like this:

```text
Controller node
  -> Hermes Fleet CLI or model tools
  -> local Keryx daemon
  -> private network or Keryx relay
  -> worker Keryx daemon
  -> fleet-node dispatcher
       -> direct handler
       -> or local Hermes Runs API
```

The controller does not need a long-running Fleet daemon. It creates a short-lived Keryx client for each operation. The worker runs `fleet-node` as a supervised service and exposes only the operations allowed by its local policy.

## Trust model

Fleet separates four questions that are often accidentally conflated:

1. **Who sent the request?** Keryx authenticated sender identity answers this.
2. **Was the sender allowed to request the operation?** Fleet and destination policy answer this.
3. **Was the request routed to the expected peer?** Keryx transport receipts answer this within their documented scope.
4. **Is the returned content safe to trust?** Peer-produced JSON and model text remain untrusted.

Authentication never makes remote content trusted. Fleet marks peer-produced direct responses and Hermes output with `untrusted: true`.

Controller-owned configuration and typed Keryx receipt fields are trusted only at their narrow boundaries. A peer response cannot override the controller-selected target, Keryx task ID, routed peer, or delivery route.

## Operation model

| Operation | Handler | Hermes run? | Purpose |
| --- | --- | ---: | --- |
| `fleet.health` | direct | No | Bounded adapter, Keryx, and Hermes API capability health |
| `fleet.inventory` | direct | No | Safe node identity, version, and capability summary |
| `fleet.message` | direct | No | Bounded text communication and acknowledgment |
| `fleet.hermes.run` | executable | Yes | One deliberate authenticated local Hermes run |

`fleet.health` may perform bounded local HTTP health and capability probes. It must never call the Hermes run-creation endpoint. `fleet.inventory` and `fleet.message` do not require a Hermes HTTP server.

## Fleet envelope

Every request is carried as one strict, versioned JSON envelope in a Keryx task message. Routing and policy-critical values are repeated in Keryx metadata and cross-checked by the destination.

```json
{
  "version": 1,
  "operation": "fleet.message",
  "target": {
    "name": "worker-1",
    "peer_id": "12D3KooW..."
  },
  "input": {
    "text": "Hello from the controller",
    "topic": "operations",
    "correlation_id": "example-1"
  },
  "limits": {
    "deadline_seconds": 30
  }
}
```

The authenticated sender comes from Keryx delivery context, never from a sender value inside the JSON payload.

The parser fails closed on:

- duplicate JSON members;
- malformed Unicode;
- non-standard numeric values;
- unknown versions or operations;
- malformed or multiple message parts;
- target or metadata disagreement;
- expired requests;
- oversized payloads;
- disabled operations.

## Controller path

The controller:

1. Loads Fleet inventory from the active Hermes environment.
2. Resolves one exact friendly name to one immutable Keryx peer ID.
3. Applies the configured request policy and limits.
4. Creates a versioned Fleet envelope and absolute deadline.
5. Submits through the public Keryx SDK.
6. Preserves the actual Keryx task ID and route receipt.
7. Waits for terminal state or reopens status by task ID.
8. Returns peer-produced content as untrusted.

Configuration alone never proves that a node is reachable. Live inventory distinguishes direct connectivity, registry visibility, absence, and unknown registry state.

Fleet does not automatically retry an ambiguous side-effecting submission. A new operator request creates a new Keryx task.

## Worker path

`fleet-node` is a supervised foreground process built on the public Keryx node API.

For each claimed task it:

1. Reads authenticated sender and destination identity from Keryx.
2. Parses the Fleet envelope.
3. Cross-checks Keryx metadata and envelope values.
4. Computes the remaining absolute deadline.
5. Applies local authorization and resource limits.
6. Selects a direct or executable handler.
7. Completes or fails the normal Keryx task/result record.

Direct communication creates no Fleet inbox or duplicate message-history database.

## Deadline handling

The absolute Keryx deadline is the source of truth.

The worker rejects expired work before starting a handler. Health probes share one remaining deadline rather than receiving independent full timeouts. Executable requests propagate the remaining budget to Hermes start, polling, and stop attempts.

Fleet must not report success after the request deadline.

## Deliberate Hermes execution

For `fleet.hermes.run`, the worker:

1. Reserves the Keryx task in a narrow execution-binding store.
2. Calls the local Hermes run-creation endpoint once.
3. Persists the returned Hermes run ID before polling.
4. Polls the exact run while Keryx maintains the claim heartbeat.
5. Fails closed if Hermes enters an approval-wait state.
6. Stores bounded terminal status and text.
7. Replays the stored result after task reclaim without creating a second Hermes run.

Fleet never auto-approves remote tool or model actions.

## Execution binding

Hermes Runs does not currently provide the durable idempotency primitive Fleet needs for reclaim safety. Fleet therefore stores a narrow SQLite correlation record keyed by Keryx task ID.

States:

- `creating`: a reservation exists, but the run ID is not durably known;
- `running`: the exact run ID is known and polling may resume;
- `completed`: bounded terminal text is available for replay;
- `indeterminate`: execution state cannot be proven and Fleet fails closed.

The binding store contains no credentials, routes, general message history, or duplicate Keryx lifecycle. It is not a second task ledger.

## Configuration and live state

Fleet configuration contains operator-authored identity and policy:

- friendly name;
- immutable Keryx peer ID;
- tags;
- enabled state;
- priority;
- allowed operations;
- request limits.

Live state comes from Keryx observations and operation responses. Fleet never treats a configured node as online merely because it appears in `nodes.yaml`.

## Public surfaces

CLI:

```text
hermes fleet init
hermes fleet list
hermes fleet show NODE
hermes fleet health NODE
hermes fleet inventory NODE
hermes fleet message NODE "TEXT"
hermes fleet run NODE "PROMPT"
hermes fleet status TASK_ID
hermes fleet cancel TASK_ID
```

Hermes model tools:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

The cancellation surface currently fails closed because origin-side cancellation cannot prove that a remote worker observed the request and stopped its bound Hermes run.

## Deployment boundaries

- Keep Keryx daemons and local Hermes APIs on loopback where possible.
- Use TLS and a private network for cross-machine relay and registry access.
- Keep node keys, tokens, TLS private keys, and Hermes credentials outside Git.
- Run one authoritative Fleet skill-registration owner per worker identity.
- Do not use compatibility bridges that read Keryx databases directly or fabricate terminal results.

## Current limitations

1. Granular destination-owned per-sender operation grants are under development.
2. Cross-node cancellation is unavailable and fails closed.
3. Relay mailbox durability depends on the deployed Keryx version.
4. Cross-node artifact bytes are not exposed by the current Fleet release.
5. Scheduling, profile lifecycle, persistent inboxes, and workflow graphs are future work.
6. Public-internet and multi-tenant deployment are not supported.
