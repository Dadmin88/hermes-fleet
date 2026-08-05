# Hermes Fleet Architecture

## Decision

Hermes Fleet v0.1 is a node communication and coordination layer built on [Hermes Keryx](https://github.com/Dadmin88/hermes-keryx). Keryx is its authenticated transport and durable task/result data plane. Direct Hermes A2A is not a transport fallback.

The responsibility boundary is:

1. **Hermes** owns local agent execution, models, tools, skills, files, credentials, permissions, memory, and sessions.
2. **Keryx** owns peer identity, relay transport, skill registration/discovery, durable task/result storage, claims, leases, heartbeats, terminal states, result routing, artifacts, cancellation records, and offline mailbox delivery.
3. **Fleet** owns friendly node names, operator tags, selection, communication envelopes, node policy, dispatch, CLI/model tools, execution binding, and operational presentation.
4. **Tailscale/private networking** is the deployment boundary around relay, registry, daemons, and local Hermes API servers.

Fleet must not implement a Keryx relay/data-plane daemon, transport protocol, task lifecycle database, independent Keryx result poller, parallel artifact channel, offline queue, WebSocket controller, SSH executor, scheduler, or workflow engine. A supervised `fleet-node` dispatcher, direct node handlers, and bounded loopback Hermes Runs polling for explicitly executable operations are Fleet responsibilities.

Keryx internally represents each request/response exchange as a task/result. Fleet-facing terminology still distinguishes communication, message, query, acknowledgment, control request, and execution request. Kanban is not a transport, queue, router, execution engine, or source of truth.

## Verified baselines

### Hermes

The public plugin contract was verified against `/home/kyle/.hermes/hermes-agent`, clean `main` at `a991dfc25` on 2026-08-05.

Fleet can use `PluginContext.register_tool`, `register_cli_command`, `register_command`, and `register_skill`. Network-bound model tools register with `is_async=True`. Plugin skills are registered with a bare name such as `fleet-operator`; Hermes publishes the qualified plugin skill name.

Focused Hermes validation passed:

```text
176 passed, 17 deselected
```

### Keryx

Fleet's current Keryx integration baseline was audited in the owned `Dadmin88/hermes-keryx` repository at product-truth commit `22d66ecf452eb6c1f87bd3710dda5ec665f5f32c` on 2026-08-05.

Source-confirmed facilities include:

- SQLite schema v7 task envelopes, claims, leases, results, result-delivery leases, artifacts, and restart recovery.
- `SubmitRemoteTask`, `ClaimNextTask`, `Heartbeat`, `CompleteTask`, `FailTask`, `GetTaskResult`, result-delivery claim/ack/fail, remote-result ingestion, cancellation, peer listing, Agent Cards, skill registration, and discovery RPCs.
- Python `KeryxNode` worker loops with concurrent claims, handler invocation, lease heartbeats, completion/failure persistence, `TaskHandle.wait()`, `TaskHandle.cancel()`, and returned artifact descriptors.
- Authenticated relay registration and sender/receiver peer validation in the two-node process harness.

## Current Keryx readiness and limits

The audited public APIs already provide authenticated registration ownership, TTL refresh/deregistration, exact-peer submission, absolute deadline transport, authenticated sender identity at claim time, immutable `routed_to`/`delivery_route` receipts, durable completed/failed text results, and same-process `TaskHandle.wait()`.

Fleet must still respect these limits:

1. **Relay mailbox persistence:** offline mailboxes are bounded in-memory queues and do not survive a relay restart.
2. **Task reopen:** a new controller process cannot yet reconstruct a public `TaskHandle` from only a known task ID. A narrow SDK reattach method is a Phase 5 candidate, not a blocker for the first two live smokes.
3. **Cross-node cancellation:** origin cancellation is public, but there is no relay cancellation frame, typed canceled terminal result, or Python worker cancellation observation. Fleet does not claim remote running-task interruption.
4. **Cross-node artifacts:** descriptor/text metadata can route, but artifact bytes remain destination-local and the high-level SDK has no download contract.
5. **Reachability semantics:** registry presence, direct connection, actual submission route, and a proven request/result round trip remain distinct states.

These limits do not justify a parallel Fleet transport, lifecycle database, cancellation protocol, or file channel.

## Fleet trust-boundary map

Exact runtime types are required where direct Python callers could supply subclasses with executable hooks. Parser-produced JSON/YAML containers remain ordinary built-ins and are validated by shape and value; exact-type checks are not spread mechanically through trusted internal helpers.

| Module | Untrusted/public boundaries and boundary helpers | Internal trusted helpers |
| --- | --- | --- |
| `config.py` | `_construct_unique_mapping`, `_require_absolute_state_root`, `get_hermes_home`, `get_fleet_dir`, `_mapping`, `_node`, `load_fleet_config` | None; every callable either receives direct caller/environment input or validates parser output. |
| `inventory.py` | `_require_absolute_state_path`, `write_json_atomic`, `write_yaml_atomic`, `_valid_cache_at`, `load_cache`, `initialize_inventory_state` | `_verify_owner_directory`, `_tighten_owner_directory`, `_open_owner_directory`, `_open_or_create_owner_directory`, `_open_state_directory`, `_safe_existing_target_at`, `_new_temporary_file`, `_atomic_write_at`, `_atomic_write`, `_read_text_at`; these operate only after concrete paths or open descriptors establish the boundary. |
| `envelope.py` | `_unique_json_object`, `_reject_nonstandard_json_value`, `_contains_surrogate_code_point`, `_validate_decoded_json_strings`, `_decode_json_payload`, `_validate_json_value`, `FleetEnvelope.to_json`, `_object`, `_positive_int`, `_export_paths`, `parse_envelope` | None; each callable validates decoded or directly constructed untrusted envelope data. |
| `models.py` | `_require_exact_type`, `_identifier`, `_peer_id`, `_positive_int`, and every `__post_init__` on `FleetDefaults`, `NodePolicy`, `NodeConfig`, and `RemoteOutput` | None; public dataclass construction is itself a boundary. |
| `policy.py` | `_positive`, `_nonnegative`, `enforce_request_policy` | None; request and collaborator values are caller-controlled. |
| `selection.py` | `_requested`, `select_nodes` | None; the public API deliberately accepts external one-shot iterables, materializes each once, then exact-checks elements. |
| `formatting.py` | `format_remote_output` | None. |

Two repeated cross-module semantic predicates are centralized narrowly: concrete platform `Path` trust in dependency-free `_paths.is_concrete_path`, and exact Fleet domain collaborators in `models._require_exact_type`. The latter is used only for `NodeConfig`, `FleetDefaults`, `NodePolicy`, and `RemoteOutput` gates before attribute access while callers retain their literal stable messages. Primitive, numeric, string, mapping, and iterable checks intentionally remain local because their accepted values, normalization, upper bounds, and errors differ. This is not a generic validation framework.

## Artifact capability boundary

### Current verified Keryx behavior

- Durable result records carry result metadata.
- Routed results may carry artifact descriptors and bounded text previews where supported.
- Artifact byte storage is destination-local.
- Fleet has no proven cross-node artifact-byte retrieval path.
- The high-level Python SDK has no artifact list/get/download contract available to Fleet.

The first safe Fleet release is text-only and has two slices: a direct `fleet.message` acknowledgment that never calls Hermes, and a deliberate `fleet.hermes.run` request that returns terminal text through Keryx.

### Deferred artifact backlog

If separately approved after the first Katana/VPS text-result release, Phase 2B would need to add and prove these upstream capabilities before Fleet advertises remote artifact retrieval:

- bounded authenticated artifact-byte transport;
- aggregate cross-node artifact limits;
- descriptor size and digest verification;
- origin-side content-addressed ingestion;
- replay-safe and duplicate-safe behavior;
- high-level Python retrieval and safe download helpers.

Binary, empty, multi-file, traversal, replay, duplicate, hash-mismatch, and oversize cases belong to that later gate. They do not block the Katana-to-VPS text-result proof.

## Runtime topology

```text
Controller device
┌──────────────────────────────────────────┐
│ Hermes                                   │
│  └─ Hermes Fleet general plugin          │
│       └─ Keryx Python SDK                │
│ local keryxd + SQLite                    │
└──────────────────┬───────────────────────┘
                   │ authenticated Fleet communication via Keryx task/result
┌──────────────────▼───────────────────────┐
│ VPS: keryx-relay + registry              │
│ - peer allowlist                         │
│ - bounded offline mailbox                │
│ - private Tailscale reachability         │
└─────────┬────────────────┬───────────────┘
          │                │
┌─────────▼────────┐ ┌─────▼──────────────┐
│ Linux/VPS node   │ │ Deferred Android   │
│ Hermes gateway   │ │ backlog only       │
│ fleet-node       │ │                    │
│ local keryxd     │ │                    │
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
      allowed_operations: [fleet.health, fleet.inventory, fleet.message, fleet.hermes.run]
      max_deadline_seconds: 900
```

Fleet never stores Keryx node private keys, relay bearer tokens, or Hermes API keys in inventory. Secrets stay in existing Keryx/Hermes credential files or environment references.

Inventory state is configuration, not a duplicate task history. Fleet obtains live peer/task/result state from Keryx. It may emit bounded operational events for selection and policy decisions, keyed by Keryx task ID, but must not create a second lifecycle database.

## Fleet communication envelope

Every Fleet communication uses a normal Keryx task/result internally, but that transport detail does not make every communication executable work. The versioned Fleet envelope is serialized as JSON into the task's sole text message part; canonical routing/policy fields also appear in Keryx metadata:

```json
{
  "version": 1,
  "operation": "fleet.message",
  "target": {
    "name": "katana",
    "peer_id": "12D3KooW..."
  },
  "input": {
    "text": "Hello from Katana",
    "topic": "smoke-test",
    "correlation_id": "corr-1"
  },
  "limits": {
    "deadline_seconds": 600
  }
}
```

Canonical Keryx metadata:

```text
fleet.envelope_version=1
fleet.operation=fleet.message
fleet.target_peer_id=12D3KooW...
fleet_deadline_ms=<absolute epoch milliseconds>
```

`fleet-node` registers one Keryx worker handler. That dispatcher validates the authenticated sender, parses the sole text part as the Fleet envelope, cross-checks target/operation/version metadata, applies local policy, and then uses one explicit dispatch table. `fleet.health`, `fleet.inventory`, and `fleet.message` route to direct handlers; only `fleet.hermes.run` routes to the Hermes execution handler.

The dispatcher rejects unknown versions, unknown operations, malformed/multiple payload parts, metadata/envelope mismatches, expired tasks, disallowed operations, and limits above local policy before invoking Hermes.

## Local Hermes execution seam

`fleet-node` is a small supervised Python execution worker built on `KeryxNode`; it is not a Keryx data-plane daemon and does not replace `keryxd`.

For `fleet.hermes.run`, the worker calls the local Hermes API server over loopback:

1. Persist `state=creating` for the Keryx task before run creation.
2. Submit authenticated `POST /v1/runs` with the Fleet prompt and bounded session correlation.
3. Persist the returned Hermes `run_id` as `state=running`.
4. Poll `GET /v1/runs/{run_id}` until terminal while the Keryx claim heartbeat continues.
5. If Hermes enters `waiting_for_approval`, request a cooperative stop and fail closed; Fleet never calls the approval endpoint or auto-approves remote work.
6. Persist bounded terminal text before returning it through the normal Keryx completion path.

The API server is a public Hermes surface with bearer authentication, runs, polling, progress, approval, and stop endpoints. The worker uses only `127.0.0.1`; the API server must not be exposed to the relay or public network. `API_SERVER_KEY` remains an environment/config secret and is never returned in Fleet output.

### Crash-safe execution binding

Hermes Runs has no durable idempotency key and run state is process-memory retained. To avoid duplicate model/tool execution after a worker crash, `fleet-node` maintains only a narrow atomic execution-binding record keyed by Keryx task ID:

1. Persist `state=creating` before `POST /v1/runs`.
2. Persist the returned Hermes `run_id` as `state=running` before normal polling.
3. On reclaim with `state=running`, resume polling that exact run; never submit another.
4. On reclaim with `state=creating`, or when the bound run is missing after Hermes restart, persist `state=indeterminate` and fail closed; never auto-resubmit.
5. Persist `state=completed` and terminal text before Keryx completion so a reclaim can replay the same result without another Hermes run.

This is an execution-correlation record, not a second Fleet task lifecycle database.

### Artifact export contract

Hermes Runs returns final text/status, not a list of created files. Fleet therefore never scans the node filesystem. Phase 1 validates `input.export_paths` syntax as a local contract, but Phase 2A sends and retrieves terminal text only. It does not claim cross-node artifact bytes or expose artifact download.

After the Phase 2B Keryx gate passes, `fleet.hermes.run` may additionally return:

- a bounded `result.txt` artifact produced from final Hermes output; and
- explicitly requested relative files under a configured export root.

`input.export_paths` is optional and contains only bounded relative paths. Future CLI/model surfaces must share that schema. Before Hermes starts, Fleet must reject absolute, empty, duplicate, `.`/`..`, control-character, and over-limit paths and create a private per-task export directory. Because Hermes may create files later, future post-run collection must repeat containment checks and open every component and final regular file relative to the task-root descriptor with no-follow semantics. Collected files must remain size/count bounded and hashed. Keryx must then provide the authenticated, aggregate-bounded, verified transport and origin ingestion described in Phase 2B; none of those future actions are present Fleet behavior today.

If a node does not expose the Runs capability, `fleet.hermes.run` is not registered. Fleet does not import Hermes private runtime internals as a fallback.

## Capabilities

The minimum node card registers:

- `fleet.health` — adapter, daemon, and local Hermes capability status; no model call.
- `fleet.inventory` — safe node identity/version/capability summary; no secrets or broad filesystem inventory.
- `fleet.message` — bounded text notice and deterministic acknowledgment; no model call.
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

## Planned CLI and model tools

After the corresponding controller phases are implemented, the CLI is planned to include:

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

Planned model-callable tools remain narrow:

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

### First-release acceptance

Hermes Fleet's first release is one Katana controller, one VPS worker, and text-only remote execution. It is not complete until a repeatable real proof shows:

- Katana selected the VPS through Fleet inventory;
- Fleet submitted the task through Keryx;
- the VPS executed the task through its local Hermes instance;
- the terminal text result returned to Katana;
- logs identify the Keryx task, peer, Hermes run, and final status;
- deadline and cooperative cancellation behavior were observed;
- GitHub Actions remained green on the released commit; and
- the two-machine smoke procedure is documented and repeatable.

Authenticated registry ownership, TTL refresh/deregistration, absolute deadline propagation, cooperative cancellation observation, and the actual-route/routed-peer submission receipt are supporting safety seams for that proof.

Artifact transport, tag fan-out, Android/Termux, mailbox durability, and richer orchestration are deferred backlog. They do not block the first release and must not interrupt the vertical slice unless a concrete critical vulnerability is found.

Fake services are acceptable for CI, never as a substitute for this release gate.
