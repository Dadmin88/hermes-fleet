# Hermes Fleet

Hermes Fleet is an experimental management and communication layer for networks of [Hermes Agent](https://github.com/NousResearch/hermes-agent) nodes. It uses Hermes Keryx for authenticated transport while Fleet provides friendly node identities, explicit operations, local policy, operator tools, and safe remote execution.

Fleet is designed around a simple rule: receiving a network message must not automatically start an AI agent. Ordinary health checks, inventory requests, and messages use direct handlers. Hermes runs only when an authorized caller deliberately requests `fleet.hermes.run`.

> **Project status:** early development. The current interfaces are usable for controlled private-network testing, but the project is not yet production-ready. Do not expose Fleet, Keryx, or local Hermes APIs directly to untrusted networks.

## What it provides

- Friendly names mapped to immutable Keryx peer IDs.
- Exact-node health, inventory, messaging, and deliberate Hermes execution.
- Strict versioned request envelopes with bounded payloads and deadlines.
- Default-deny operation policy for every configured node.
- Live reachability derived from Keryx rather than configuration alone.
- Durable Keryx task status and terminal text retrieval.
- Narrow task-to-Hermes-run binding to prevent duplicate remote execution.
- A Hermes CLI, model tools, operator skill, and reference systemd units.

## Operations

| Operation | Starts Hermes? | Purpose |
| --- | ---: | --- |
| `fleet.health` | No | Bounded adapter, Keryx, and local Hermes API capability health |
| `fleet.inventory` | No | Safe node identity, version, and capability summary |
| `fleet.message` | No | Bounded text communication with an acknowledgment |
| `fleet.hermes.run` | Yes | One deliberate local Hermes run on the selected node |

All peer-produced responses and model output are returned as untrusted data. Authentication identifies the sender; it does not make the sender's content safe to execute or follow.

## Architecture

```text
Operator or Hermes model tool
        |
        v
Hermes Fleet controller
        |
        v
Keryx authenticated task/result transport
        |
        v
fleet-node dispatcher
    |                 |
    | direct          | executable
    v                 v
health / inventory    local Hermes Runs API
message handlers      one bounded Hermes run
```

Responsibilities stay deliberately separate:

- **Hermes Agent** owns local models, tools, skills, sessions, credentials, and execution.
- **Keryx** owns peer identity, discovery, routing, delivery, durable task/result state, leases, deadlines, and transport receipts.
- **Hermes Fleet** owns friendly node configuration, operation policy, request envelopes, dispatch, execution binding, CLI/model tools, and presentation.
- **Private networking** such as Tailscale limits which machines can reach Fleet and Keryx services.

See [Architecture](docs/architecture.md) for the complete boundary.

## Installation

Hermes Fleet is packaged as a Hermes Git-directory plugin.

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

Restart an already-running Hermes gateway after installing or updating the plugin so it loads the Fleet tools:

```bash
hermes gateway restart
```

`hermes fleet init` creates missing Fleet state without overwriting valid configuration.

## Configuration

Fleet stores its operator inventory at `HERMES_HOME/fleet/nodes.yaml` for the active Hermes environment.

```yaml
schema_version: 1

defaults:
  max_deadline_seconds: 300
  max_payload_bytes: 65536
  max_prompt_chars: 16000
  max_export_paths: 8

nodes:
  - name: worker-1
    peer_id: "12D3KooW..."
    tags: [worker]
    enabled: true
    priority: 0
    policy:
      allowed_operations:
        - fleet.health
        - fleet.inventory
        - fleet.message
      max_deadline_seconds: 120
      max_payload_bytes: 65536
      max_prompt_chars: 16000
      max_export_paths: 0
```

The inventory contains no URLs, bearer tokens, private keys, or model credentials. Keryx connection details and secrets belong in owner-protected runtime configuration outside Git.

## CLI

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

Example:

```bash
hermes fleet health worker-1
hermes fleet message worker-1 "Hello from the controller" --topic operations
hermes fleet run worker-1 "Return exactly READY"
```

Cross-node cancellation currently fails closed because Fleet cannot yet prove that a remote worker observed the request and stopped its bound Hermes run.

## Hermes model tools

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

See [Operator skill](SKILL.md) for safe tool-selection guidance.

## Security model

Hermes Fleet is intended for controlled private networks.

- Keep Keryx daemons and local Hermes APIs on loopback where possible.
- Use an encrypted private network for cross-machine reachability.
- Never commit node keys, tokens, TLS private keys, API keys, or model credentials.
- Treat remote JSON and model text as untrusted input.
- Keep worker policies default-deny and enable only required operations.
- Do not use legacy bridges that read Keryx databases directly or fabricate task results.
- Do not interpret a transport receipt as proof that remote execution completed.

Security issues should be reported according to [SECURITY.md](SECURITY.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment guide](docs/deployment.md)
- [Two-node smoke test](docs/smoke-test.md)
- [Operator skill](SKILL.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Current limitations

- Granular destination-owned per-sender grants are under development.
- Cross-node cancellation is unavailable and reported honestly.
- Relay offline mailbox durability depends on the deployed Keryx version.
- Cross-node artifact bytes, scheduling, profile lifecycle, persistent inboxes, workflow graphs, and multi-tenant operation are not part of the current release.
- Public-internet deployment has not been hardened or accepted.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
python -m build
```

Hermes Fleet is licensed under the [MIT License](LICENSE).
