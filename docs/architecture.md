# Hermes Fleet Architecture

## Decision

Hermes Fleet v0.1 uses [Hermes Keryx](https://github.com/DeployFaith/hermes-keryx) as its durable task transport and data plane. Direct Hermes A2A is not the primary transport.

The responsibility boundary is:

1. **Hermes** owns local agent execution, models, tools, skills, files, credentials, permissions, memory, and sessions.
2. **Keryx** owns peer identity, relay transport, skill registration/discovery, durable task/result storage, claims, leases, heartbeats, terminal states, result routing, artifacts, cancellation records, and offline mailbox delivery.
3. **Fleet** owns friendly node names, operator tags, selection, Hermes-specific envelopes, node policy, CLI/model tools, and operational presentation.
4. **Tailscale/private networking** is the deployment boundary around relay, registry, daemons, and local Hermes API servers.

Fleet must not implement a Keryx relay/data-plane daemon, transport protocol, task lifecycle database, independent Keryx result poller, parallel artifact channel, offline queue, WebSocket controller, SSH executor, scheduler, or workflow engine. A supervised `fleet-node` execution adapter and its bounded loopback Hermes Runs polling are Fleet responsibilities.

## Verified baselines

### Hermes

The public plugin contract was verified against `/home/kyle/.hermes/hermes-agent`, clean `main` at `a991dfc25` on 2026-08-05.

Fleet can use `PluginContext.register_tool`, `register_cli_command`, `register_command`, and `register_skill`. Network-bound model tools register with `is_async=True`. Plugin skills are registered with a bare name such as `fleet-operator`; Hermes publishes the qualified plugin skill name.

Focused Hermes validation passed:

```text
176 passed, 17 deselected
```

### Keryx

Keryx was audited at `DeployFaith/hermes-keryx` `main` commit `97fcb5d` on 2026-08-05. Phase 17 was implemented by merged PR #29 (`906823badac04fd9d159c4da927dda5c25d712dc`) and issue #10 is closed.

Source-confirmed facilities include:

- SQLite schema v7 task envelopes, claims, leases, results, result-delivery leases, artifacts, and restart recovery.
- `SubmitRemoteTask`, `ClaimNextTask`, `Heartbeat`, `CompleteTask`, `FailTask`, `GetTaskResult`, result-delivery claim/ack/fail, remote-result ingestion, cancellation, peer listing, Agent Cards, skill registration, and discovery RPCs.
- Python `KeryxNode` worker loops with concurrent claims, handler invocation, lease heartbeats, completion/failure persistence, `TaskHandle.wait()`, `TaskHandle.cancel()`, and returned artifact descriptors.
- Authenticated relay registration and sender/receiver peer validation in the two-node process harness.

## Current Keryx boundaries Fleet must respect

The audit found several narrower limits than the former product docs implied:

1. **Relay mailbox persistence:** offline mailboxes are bounded in-memory queues. They survive a node disconnect/reconnect while the relay stays up, but not a relay restart.
2. **SDK skill tags:** registry protocol records support skill tags, but the current Python `Skill` and `register_skills()` path does not propagate them. Fleet operator tags therefore remain in Fleet inventory until a small SDK tag slice is added.
3. **Remote task deadlines:** local Keryx `TaskRecord.deadline_ms` storage/enforcement exists, but `TaskEnvelope` has no deadline field and remote acceptance constructs records without one. Fleet needs an absolute deadline wire field propagated through SDK, daemon, relay, destination acceptance, and store before claiming remote deadline enforcement.
4. **Cancellation execution:** `TaskHandle.cancel()` persists and routes cancellation, but the Python worker handler does not currently receive a cooperative cancellation signal. A cancelled Keryx task can therefore leave local Hermes work running until it exits. Fleet needs a narrow worker cancellation hook before claiming end-to-end stop behavior.
5. **Cross-node artifacts:** daemon artifact CRUD is local to each node. Phase 17 routes descriptors and bounded text previews, not artifact bytes, to the origin; the high-level Python SDK also lacks artifact list/get/download helpers. Fleet cannot claim remote artifact retrieval until Keryx adds content/reference transport and SDK access.
6. **Reachability semantics:** registry presence, direct peer connection, the actual route reported after submission, and a proven task/result round trip are different states. The high-level Python SDK currently discards `SendTaskResponse.delivery_route`/`routed_to`, so Fleet needs a narrow submission-receipt extension and must not pre-label a node as mailbox-eligible.
7. **Registry ownership/authentication:** relay task/result control can require node tokens, but the separate registry gRPC surface currently allows unauthenticated register/replace/unregister calls. Fleet deployment needs authenticated peer-owned mutation plus Tailscale isolation; network privacy alone is not authorization.
8. **Registration lifecycle:** Python registration is one-shot with a default 300-second TTL. `fleet-node` must refresh before expiry and deregister on graceful shutdown.

These are upstream integration slices, not justification for a parallel Fleet transport, database, or file channel.

## Runtime topology

```text
Controller device
┌──────────────────────────────────────────┐
│ Hermes                                   │
│  └─ Hermes Fleet general plugin          │
│       └─ Keryx Python SDK                │
│ local keryxd + SQLite                    │
└──────────────────┬───────────────────────┘
                   │ authenticated Keryx task/result
┌──────────────────▼───────────────────────┐
│ VPS: keryx-relay + registry              │
│ - peer allowlist                         │
│ - bounded offline mailbox                │
│ - private Tailscale reachability         │
└─────────┬────────────────┬───────────────┘
          │                │
┌─────────▼────────┐ ┌─────▼──────────────┐
│ Linux/VPS node   │ │ Android/Termux node│
│ Hermes gateway   │ │ Hermes gateway     │
│ fleet-node       │ │ fleet-node         │
│ local keryxd     │ │ local keryxd       │
└──────────────────┘ └────────────────────┘
```

A workstation can host both controller and worker roles without merging their responsibilities.

## Fleet inventory model

Keryx `peer_id` is the immutable transport identity. Fleet stores only operator metadata:

```yaml
schema_version: 1
nodes:
  - name: katana
    peer_id: 12D3KooW...
    tags: [workstation, gpu]
    enabled: true
    priority: 100
    policy:
      allowed_operations: [fleet.health, fleet.inventory, fleet.hermes.run]
      max_deadline_seconds: 900
      max_parallel: 1
```

Fleet never stores Keryx node private keys, relay bearer tokens, or Hermes API keys in inventory. Secrets stay in existing Keryx/Hermes credential files or environment references.

Inventory state is configuration, not a duplicate task history. Fleet obtains live peer/task/result state from Keryx. It may emit bounded operational events for selection and policy decisions, keyed by Keryx task ID, but must not create a second lifecycle database.

## Fleet task envelope

Every Fleet request is a normal Keryx task. The versioned Fleet envelope is serialized as JSON into the task's sole text message part; canonical routing/policy fields also appear in Keryx metadata:

```json
{
  "version": 1,
  "operation": "fleet.hermes.run",
  "input": {
    "prompt": "Run the focused tests and summarize failures.",
    "session_id": null,
    "instructions": null,
    "export_paths": ["reports/focused-tests.txt"]
  },
  "limits": {
    "deadline_seconds": 600
  }
}
```

Canonical Keryx metadata:

```text
skill_id=fleet.hermes.run
capability_id=fleet.hermes.run
fleet.envelope_version=1
fleet.operation=fleet.hermes.run
```

`fleet-node` registers one Keryx worker handler. That dispatcher validates `target_skill_id`, parses the sole text part as the Fleet envelope, cross-checks operation metadata, and then calls the operation-specific handler. Keryx handlers are not registered per skill, so registering three independent handlers would be unsafe.

The dispatcher rejects unknown versions, unknown operations, malformed/multiple payload parts, metadata/envelope mismatches, expired tasks, disallowed operations, and limits above local policy before invoking Hermes.

## Local Hermes execution seam

`fleet-node` is a small supervised Python execution worker built on `KeryxNode`; it is not a Keryx data-plane daemon and does not replace `keryxd`.

For `fleet.hermes.run`, the worker calls the local Hermes API server over loopback:

1. Probe authenticated `GET /v1/capabilities`.
2. Submit `POST /v1/runs` with the Fleet prompt and optional session metadata.
3. Poll `GET /v1/runs/{run_id}` until terminal while the Keryx claim heartbeat continues.
4. If cooperative Keryx cancellation is observed, call `POST /v1/runs/{run_id}/stop`.
5. If Hermes enters `waiting_approval`, stop/fail the run as `approval_required`; Fleet never calls the approval endpoint or auto-approves remote work.
6. Convert the terminal Hermes run into Keryx result metadata and artifact descriptors.

The API server is a public Hermes surface with bearer authentication, runs, polling, progress, approval, and stop endpoints. The worker uses only `127.0.0.1`; the API server must not be exposed to the relay or public network. `API_SERVER_KEY` remains an environment/config secret and is never returned in Fleet output.

### Crash-safe execution binding

Hermes Runs has no durable idempotency key and run state is process-memory retained. To avoid duplicate model/tool execution after a worker crash, `fleet-node` maintains only a narrow atomic execution-binding record keyed by Keryx task ID:

1. Persist `state=preparing` before `POST /v1/runs`.
2. Persist the returned Hermes `run_id` as `state=running` before normal polling.
3. On reclaim with `state=running`, resume polling that exact run; never submit another.
4. On reclaim with `state=preparing`, or when the bound run is missing after Hermes restart, fail closed as `execution_uncertain`; never auto-resubmit.
5. Remove the binding only after one terminal Keryx completion/failure is durably accepted.

This is an execution-correlation record, not a second Fleet task lifecycle database.

### Artifact export contract

Hermes Runs returns final text/status, not a list of created files. Fleet therefore never scans the node filesystem. `fleet.hermes.run` may return:

- a bounded `result.txt` artifact produced from final Hermes output; and
- explicitly requested relative files under a configured export root.

`input.export_paths` is optional, contains only bounded relative paths, and is exposed by `hermes fleet run --export <relative-path>` and the model tool's `export_paths` array through one shared schema. Before Hermes starts, Fleet rejects absolute, empty, duplicate, `.`/`..`, control-character, and over-limit paths and creates a private per-task export directory. Because Hermes may create the files later, post-run collection repeats containment checks and opens every directory component and final regular file relative to the task-root directory descriptor with no-follow semantics; it never trusts a preflight path resolution. Collected files are size/count bounded, hashed, and handed to Keryx. Keryx routes authenticated artifact bytes inline in the result envelope with a default **4 MiB aggregate cross-node limit**, ingests them into the origin daemon's existing content-addressed artifact store, and exposes safe Python download helpers. The existing 256 MiB node-local blob ceiling does not imply a 256 MiB relay payload.

If a node does not expose the Runs capability, `fleet.hermes.run` is not registered. Fleet does not import Hermes private runtime internals as a fallback.

## Capabilities

The minimum node card registers:

- `fleet.health` — adapter, daemon, and local Hermes capability status; no model call.
- `fleet.inventory` — safe node identity/version/capability summary; no secrets or broad filesystem inventory.
- `fleet.hermes.run` — bounded local Hermes run through the loopback Runs API.

Keryx skill discovery is the live capability source. Fleet operator tags are independent selection metadata. A later Keryx SDK tag slice may publish capability tags without changing Fleet selection semantics.

## Selection and dispatch

1. Resolve exact friendly names or all operator tags from Fleet inventory.
2. Reject disabled, duplicate, unknown, or policy-ineligible nodes before submission.
3. Intersect configured nodes with Keryx discovery/peer state.
4. Sort direct-connected before registry-visible before unknown/offline, then priority descending and name ascending. Do not infer mailbox eligibility before submission.
5. Submit one Keryx task per selected node with bounded concurrency.
6. Wait through Keryx `TaskHandle.wait()` and preserve partial success.
7. Return Keryx task IDs, actual submission route, terminal status, result metadata, and artifact descriptors.

There is no automatic retry after ambiguous submission. Repeated submissions require a new Fleet request and produce new Keryx task IDs.

## Policy layers

- **Relay allowlist:** who may join/routably exchange frames.
- **Keryx routing policy:** peer/capability allow, deny, or approval decisions before routing.
- **Fleet controller policy:** which friendly targets and operations the operator may request.
- **Fleet node policy:** which authenticated peers/operations/limits may invoke local Hermes.
- **Hermes:** tool permissions and approval behavior during execution.

Fleet never promotes an authenticated remote request into an approval decision. Human approval remains local to Hermes/operator policy.

Every node is default-deny. Only explicitly listed authenticated sender peer IDs and `fleet.*` operation IDs may reach the dispatcher; rejection occurs before any Hermes Runs request.

Authentication never implies authorization.

## CLI and model tools

CLI:

```text
hermes fleet init
hermes fleet node add|remove|enable|disable|tag
hermes fleet list [--refresh] [--json]
hermes fleet show NODE [--json]
hermes fleet run NODE --prompt ... [--deadline ...]
hermes fleet run --tag TAG --prompt ... [--max-parallel ...]
hermes fleet task TASK_ID [--wait]
hermes fleet cancel TASK_ID
hermes fleet artifacts TASK_ID --output DIR
hermes fleet doctor [--deep] [--json]
```

Model-callable tools remain narrow:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`
- `fleet_get_artifacts`

Tools accept configured names/tags and Keryx task IDs, never arbitrary URLs, shell commands, peer private keys, or tokens.

## Direct Hermes A2A

Native Hermes A2A remains a verified compatibility surface, but it is outside the Fleet v0.1 data path. A future adapter may translate Fleet envelopes to A2A only behind the same selection/policy boundary. Fleet v0.1 contains no direct HTTP A2A transport or fallback.

## Release boundary

### Existing Katana migration hazard

The current Katana `keryx-task-bridge.service` is a pre-Phase-17 stub. It reads pending task IDs directly from Keryx SQLite, claims them, and completes them immediately with `"Agency node processing not yet wired."` It must be disabled and replaced by `fleet-node` before the relay is activated for Fleet traffic. Fleet must never read or mutate Keryx SQLite directly.

Fleet v0.1 is not complete until one real controller dispatches through Keryx to three independent Hermes installations—workstation, VPS/Linux, and Android/Termux—and verifies:

- distinct stable peer IDs and friendly names;
- capability discovery;
- one named-node run;
- one tag-selected parallel run;
- durable result retrieval after sender waiting is interrupted;
- artifact retrieval;
- policy denial;
- deadline behavior;
- node-offline mailbox behavior without claiming relay-restart durability;
- cancellation behavior at the level actually implemented.

Fake services are acceptable for CI, never as a substitute for this release gate.
