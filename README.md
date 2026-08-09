# Hermes Fleet

Hermes Fleet is the coordination and control layer for a network of Hermes-capable nodes. It gives operators stable node identity, exact-node selection, bounded communication envelopes, local authorization policy, remote status, and deliberate Hermes execution while delegating authenticated transport and durable task/result delivery to [Hermes Keryx](https://github.com/Dadmin88/hermes-keryx).

Fleet is intentionally not a second transport system. Keryx moves and persists authenticated work; Fleet decides what a named node may do with that work and how operators interact with it.

> Remote Hermes execution is an explicit Fleet capability. Receiving a Fleet message never implies permission to start Hermes.

## Capabilities

Fleet currently provides:

- friendly operator-managed names mapped to immutable Keryx peer identities;
- deterministic exact-node selection;
- bounded, versioned request envelopes and deadlines;
- default-deny per-node operation policy;
- direct health, inventory, and message operations;
- deliberate remote Hermes execution through the local Hermes Runs API;
- durable task-to-run binding so reclaimed work does not silently create duplicate Hermes runs;
- durable task status reattachment through Keryx;
- local managed-state projection from Nodescale with operator deny precedence;
- CLI commands, Hermes model tools, an operator skill, and systemd deployment units.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| **Hermes Fleet** | Friendly node identity, exact-node selection, request envelopes, local authorization, dispatch, execution binding, operator tools, and presentation. |
| **Hermes Keryx** | Authenticated peer identity, discovery, routing, durable task/result state, claims, leases, result delivery, cancellation records, and relay behavior. |
| **Nodescale** | Managed device membership and desired managed Fleet state. |
| **Hermes** | Local agent execution, models, tools, skills, permissions, memory, and sessions. |

Mesh membership, Keryx authentication, Fleet authorization, and Hermes execution are separate gates. No hostname, tag, peer-produced response field, or managed role automatically grants `fleet.hermes.run`.

## Operations

| Operation | Hermes run | Purpose |
| --- | ---: | --- |
| `fleet.health` | No | Bounded Fleet, Keryx, and local Hermes capability health. |
| `fleet.inventory` | No | Safe node identity, version, and capability summary. |
| `fleet.message` | No | Bounded text communication with deterministic acknowledgement. |
| `fleet.hermes.run` | Yes | Deliberately start and observe one authenticated local Hermes run. |

All peer-originated content is treated as untrusted data even when Keryx authenticated the sender. Authentication establishes who sent a response, not whether its contents are safe to trust as local configuration or authority.

## Implementation

Hermes Fleet currently contains both the proven Python plugin/runtime and the growing Rust implementation under the same product and repository.

The Python implementation remains the operational integration surface and behavioral compatibility reference. The Rust workspace currently provides durable domain and state foundations and is intended to assume more of the permanent runtime over time without changing the product identity or external contracts.

Repository layout:

```text
hermes_fleet/           Python Fleet plugin/runtime
crates/fleet-domain/    Rust domain types and authorization semantics
crates/fleet-state/     Rust durable Fleet state
fixtures/               Language-neutral compatibility fixtures
ops/                    Service and deployment assets
docs/                   Architecture, deployment, projection, and verification docs
SKILL.md                 Hermes operator skill
```

## Install as a Hermes plugin

Git must already be authorized to access this repository.

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

Restart an already-running Hermes gateway after installing or updating the plugin so that process loads the current Fleet model tools:

```bash
hermes gateway restart
```

`hermes fleet init` creates missing Fleet state without overwriting valid operator-managed state.

## Operator CLI

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

Cross-node cancellation currently fails closed because Fleet cannot yet prove that the destination worker observed cancellation and stopped an already-bound Hermes run.

## Development

Python checks:

```bash
python -m pytest
python -m ruff check .
```

Rust checks:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --workspace
```

Use the language-neutral fixtures when changing behavior shared by the Python and Rust implementations.

## Documentation

Start with the [documentation index](docs/README.md).

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Managed projection V1](docs/managed-projection-v1.md)
- [Integration verification](docs/smoke-test.md)
- [Operator skill](SKILL.md)
- [Changelog](CHANGELOG.md)

Implementation chronology, checkpoint hashes, machine-specific rollout notes, and agent plans belong in pull requests, issues, releases, or local workspace state rather than durable product documentation.

## Known limitations

- Cross-node running-task cancellation is not yet proven end to end and therefore remains unavailable.
- Keryx relay offline mailbox durability is a Keryx concern; Fleet does not add a second mailbox or queue.
- Cross-node artifact transfer, fan-out, pub/sub, broadcast, persistent inboxes, multi-node chat, workflow graphs, and multi-tenant control are outside the current Fleet surface.
- A running Hermes gateway must be restarted after a plugin update before that process sees newly installed Fleet model tools.

## License

Hermes Fleet is licensed under the GNU Affero General Public License v3.0 only (`AGPL-3.0-only`). See [LICENSE](LICENSE).

Code published in earlier commits under a different license remains available under the terms that applied when it was published.
