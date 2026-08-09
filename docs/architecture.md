# Hermes Fleet Architecture

## Decision

Hermes Fleet v0.1 is a node communication and coordination layer built on [Hermes Keryx](https://github.com/Dadmin88/hermes-keryx). Keryx is the authenticated transport and durable task/result data plane. Direct Hermes A2A is not a v0.1 transport or fallback.

## Managed projection V1 (N7 contract; not accepted)

The minimal N7 managed-projection contract is a separate, local Nodescale-to-Fleet interface. Fleet accepts its length-prefixed, strict `fleet.managed-projection.v1` JSON documents only on an authenticated Linux UDS after exact `SO_PEERCRED` verification of the configured Nodescale UID. It persists generated membership/enrollment/grants in a dedicated Fleet-owned store, separate from operator state and the v0.1 Keryx/task-to-run stores.

Generated grants are limited to `fleet.health`, `fleet.inventory`, and `fleet.message`; a local operator deny always wins; and no managed projection grants `fleet.hermes.run`. Generation/hash application and authoritative durable read-back are specified in [Managed projection V1](managed-projection-v1.md). This is a contract for future implementation and acceptance, not a claim that the existing v0.1 deployment provides it.

The responsibility boundary is:

1. **Hermes** owns local agent execution, models, tools, skills, files, credentials, permissions, memory, and sessions.
2. **Keryx** owns peer identity, registration/discovery, relay transport, routing, durable task/result storage, claims, leases, heartbeats, terminal states, result routing, cancellation records, and offline mailbox delivery.
3. **Fleet** owns friendly node names, operator metadata, exact-node selection, communication envelopes, node policy, dispatch, CLI/model tools, execution binding, and operational presentation.
4. **Tailscale/private networking** is the deployment boundary around relay, registry, daemons, and local Hermes API servers.

Fleet does not implement another relay, transport protocol, task lifecycle database, result poller, offline queue, WebSocket controller, SSH executor, scheduler, workflow engine, or artifact channel. Kanban is not a transport, queue, router, execution engine, or source of truth.

## Accepted release baseline

The first Katana-to-VPS release was accepted on 2026-08-05 against:

- Fleet runtime code: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`
- Keryx integration code: `f4ee645e415600a959ea8062d1143140bd6c2616`
- Keryx integration PR: [Dadmin88/hermes-keryx#36](https://github.com/Dadmin88/hermes-keryx/pull/36)
- Hermes plugin compatibility baseline: `a991dfc25daf68994c21d6adcdfbafb1b3dc23cf`

Fleet CI run `31062104463` passed the Python 3.11 and 3.13 quality jobs plus the complete clean-install Hermes plugin smoke. The accepted live paths used the relay route and proved ordinary communication, deliberate execution, durable result reattachment, duplicate prevention, and deadline-bounded health probing.

## Runtime topology

```text
Katana Hermes profile: katana
  -> Fleet CLI/model controller
  -> local keryxd on loopback
  -> Katana Keryx edge node
  -> authenticated relay on the VPS over Tailscale
  -> VPS Keryx edge node
  -> VPS local keryxd on loopback
  -> fleet-node dispatcher
       -> direct handler
       -> or VPS Hermes admin Runs API on 127.0.0.1:8642
```

Katana's local `vps` Hermes profile is a client-side route to the VPS gateway. The remote Fleet worker uses the VPS `admin` profile for the Hermes Runs API and Fleet state.

## Trust model

Authenticated peer identity and trusted content are separate properties.

Keryx authenticates which peer sent a communication. It does not make peer-produced JSON or model text trusted. Fleet therefore returns `untrusted: true` for:

- `fleet.health` responses;
- `fleet.inventory` responses;
- `fleet.message` acknowledgments;
- `fleet.hermes.run` terminal text;
- reopened remote `result_text` values.

Controller-owned configuration and Keryx-provided typed submission receipt fields are interpreted only at their specific trusted boundaries. Peer response fields cannot override the selected target, Keryx task ID, routed peer, or delivery route.

Authentication never implies authorization. Every worker is default-deny and validates the authenticated sender peer, destination peer, operation, envelope version, canonical Keryx metadata, deadline, payload limits, and local operation policy before dispatch.

## Operation model

Keryx internally transports every exchange as a task/result. Fleet-facing terminology distinguishes messages, queries, acknowledgments, status requests, and executable work.

The v0.1 operation table is explicit:

| Operation | Handler class | Hermes run created? | Purpose |
| --- | --- | ---: | --- |
| `fleet.health` | direct | No | Bounded adapter, Keryx, and Hermes API capability health |
| `fleet.inventory` | direct | No | Safe node identity, version, and capability summary |
| `fleet.message` | direct | No | Bounded text communication and deterministic acknowledgment |
| `fleet.hermes.run` | executable | Yes | One deliberate authenticated loopback Hermes run |

`fleet.health` may call bounded `GET /health` and authenticated `GET /v1/capabilities` probes. It never calls `POST /v1/runs`. `fleet.inventory` and `fleet.message` do not depend on the Hermes HTTP server.

Only a validated, authorized `fleet.hermes.run` reaches the executable handler.

## Fleet communication envelope

Every Fleet request uses one strict schema-v1 JSON envelope in the Keryx task's sole text message part. Canonical routing and policy values are repeated in Keryx metadata and cross-checked at the worker.

Example:

```json
{
  "version": 1,
  "operation": "fleet.message",
  "target": {
    "name": "vps",
    "peer_id": "12D3KooW..."
  },
  "input": {
    "text": "Hello from Katana",
    "topic": "smoke-test",
    "correlation_id": "corr-1"
  },
  "limits": {
    "deadline_seconds": 30
  }
}
```

Canonical Keryx metadata includes the Fleet envelope version, operation, target peer ID, skill, and absolute deadline. The authenticated sender identity comes from the Keryx delivery context, never from a sender value supplied inside JSON.

The parser rejects duplicate object members, non-standard numeric values, malformed Unicode, unknown versions or operations, malformed/multiple parts, metadata/envelope disagreement, invalid target identity, expired requests, oversized fields, and disallowed operations.

## Controller path

The controller:

1. Loads schema-v1 Fleet inventory from the active Hermes profile.
2. Resolves one exact friendly node name to an immutable Keryx peer ID.
3. Applies the configured operation and limit policy.
4. Builds one Fleet envelope with an absolute deadline.
5. Submits through the public Keryx Python SDK.
6. Preserves Keryx's actual `task_id`, `routed_to`, and `delivery_route` receipt.
7. Waits through the public task handle or reopens durable status by task ID.
8. Returns peer-produced content as untrusted.

Configuration alone never proves reachability. Live inventory distinguishes direct-connected, registry-visible, not-visible, and unknown states. The actual route is known only after Keryx returns a submission receipt.

There is no automatic retry after ambiguous submission. A new operator request creates a new Keryx task ID.

## Worker path

`fleet-node` is one supervised foreground worker built on the public `KeryxNode` API. It registers the four Fleet operations as one card and uses one explicit dispatcher.

For every claimed task it:

1. Reads authenticated sender and destination identity from Keryx.
2. Parses the single Fleet envelope.
3. Cross-checks Keryx metadata and envelope values.
4. Computes the remaining absolute deadline.
5. Applies local policy and size limits.
6. Dispatches to a direct handler or the executable handler.
7. Completes or fails the normal Keryx task/result record.

Direct communication is stateless from Fleet's perspective. No Fleet inbox or message-history database is created.

## Deadline handling

The Keryx absolute deadline is the source of truth.

The worker rejects already-expired work before starting a handler. `fleet.health` shares one remaining deadline across both synchronous Hermes HTTP probes, recomputes the remaining budget between calls, and wraps the thread call with `asyncio.wait_for` so the Fleet task cannot complete after its deadline.

The production `HermesRunsClient` also propagates the remaining HTTP timeout. A custom thread implementation that ignores its own timeout cannot be forcibly killed by `asyncio.wait_for`; this is a documented Python runtime limitation, not the production client behavior.

## Deliberate Hermes execution

For `fleet.hermes.run`, the worker uses the loopback authenticated Hermes Runs API:

1. Reserve the Keryx task in the execution-binding store as `creating`.
2. Call `POST /v1/runs` exactly once.
3. Persist the returned Hermes run ID as `running` before polling.
4. Poll the exact run until terminal while Keryx maintains the task claim heartbeat.
5. Stop and fail closed if Hermes enters an approval-wait state; Fleet never auto-approves.
6. Persist bounded terminal status/text as `completed` before completing the Keryx result.
7. Replay a stored terminal result to Keryx after reclaim without creating another Hermes run.

## Crash-safe execution binding

Hermes Runs has no durable idempotency key and its live run state is process-memory retained. Fleet therefore keeps one narrow SQLite correlation ledger keyed by Keryx task ID.

States:

- `creating`: reservation exists but the run ID is not durably known;
- `running`: the exact Hermes run ID is known and polling may resume;
- `completed`: bounded terminal text is stored for idempotent Keryx replay;
- `indeterminate`: execution state cannot be proven and Fleet fails closed.

The store contains no prompt, credential, route history, duplicate Keryx lifecycle, or user-facing message history. It is not a second task database.

## Public surfaces

Implemented CLI:

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

Implemented async model tools:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

The cancellation surface intentionally returns an unavailable error. Origin-side cancellation cannot currently prove that the remote worker observed cancellation and stopped its bound Hermes run.

## Live acceptance

### Direct communication

- Keryx task: `7e78f4c1-240a-496f-bbf4-2a0a491018d6`
- Operation: `fleet.message`
- Text: `FLEET_MESSAGE_OK`
- Route: `relay`
- VPS acknowledgment: `received`
- Hermes runs created: `0`
- Binding rows created: `0`
- Peer response presentation: `untrusted: true`

### Deliberate execution

- Keryx task: `913af216-2866-48e8-8f18-b479df479466`
- Hermes run: `run_b9f345d82c3d45778b14714966922f7e`
- Route: `relay`
- Terminal result: `FLEET_OK`
- Durable binding: `completed`
- Reattached status: `completed`, result `FLEET_OK`

Live health, inventory, list, and durable status retrieval also passed. The late exact-byte review corrected the peer-output trust flag and shared health deadline before final acceptance.

## Deployment boundaries

- Relay registry/control endpoints use TLS on the VPS Tailscale address.
- Keryx daemons and the Hermes Runs API bind to loopback.
- The relay libp2p listener binds to the VPS Tailscale address.
- Node keys, node tokens, TLS private keys, and Hermes API credentials never belong in Git.
- `fleet-node` is the sole Fleet skill-registration owner on the VPS.
- The historical `keryx-task-bridge.service` and refresh-loop service are disabled and inactive.

## Known limits

1. Cross-node running-task cancellation is unavailable and fails closed.
2. Relay offline mailboxes are bounded in-memory queues and do not survive relay restart.
3. Cross-node artifact bytes are not exposed by Fleet v0.1.
4. The Tailscale TLS certificate expires on 2026-09-17 and requires renewal plus relay restart.
5. A running Katana Hermes gateway must be restarted after plugin updates to load new Fleet model tools.
6. `node_service.py` may call `node.stop()` twice during normal shutdown; the call is outside the accepted communication/execution paths and is tracked as nonblocking cleanup.

## Deferred backlog

Deferred work includes cross-node cancellation transport, artifacts, tag fan-out, partial-result orchestration, pub/sub, broadcast, persistent inboxes, multi-node chat, agent-session routing, workflow graphs, Kanban integration, Android/Termux, public-internet exposure, and multi-tenant architecture.
