# Hermes Fleet

Hermes Fleet is the coordination and control layer for a network of Hermes-capable nodes. It gives operators stable node identity, exact-node selection, bounded communication envelopes, local authorization policy, remote status, deliberate Hermes execution, readiness awareness, and a growing profile-placement foundation while delegating authenticated transport and durable task/result delivery to [Hermes Keryx](https://github.com/Dadmin88/hermes-keryx).

Fleet is intentionally not a second transport system. Keryx moves and persists authenticated work; Fleet decides what a named node may do with that work, whether it is currently ready, and how operators and agents interact with the distributed system.

> Remote Hermes execution is an explicit Fleet capability. Receiving a Fleet message never implies permission to start Hermes.

## Where Fleet fits

Fleet sits in the middle of a deliberately layered Hermes ecosystem:

```text
Headscale / Tailscale   private connectivity
        ↓
Hermes Nodescale        device identity, trust, admission
        ↓
Hermes Fleet            control, authorization, readiness, selection
        ↓
Hermes Keryx            authenticated transport + durable tasks/results
        ↓
fleet-node              operation validation and dispatch on each machine
        ↓
Hermes Agent             local profiles, tools, sessions, Runs execution

Hermes Agency           versioned professional profile packages
        ↓
Hermes Agent installs them; Fleet observes where they are actually present
```

The order above is a map of responsibility, not a statement that every control path is physically chained through every layer. For example, Nodescale projects managed state into Fleet through a local authenticated control interface, while Keryx carries cross-node Fleet work.

For the full map, including diagrams, trust gates, task flow, profile placement, and current-versus-planned boundaries, start with the [Hermes Fleet ecosystem map](docs/ecosystem.md).

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
- persisted layered node observations with freshness, Hermes worker capacity, resources, and explainable scheduler readiness;
- observed installed Hermes profile distributions as part of current node state;
- exact `hermes-agency-profile-content.v1` package digests when a profile's Agency identity can be safely proven;
- deterministic ready-node lookup for requested profiles, including exact package lookup;
- pinned immutable Hermes Agency snapshot/package validation and read-only eligible placement-candidate discovery;
- CLI commands, Hermes model tools, an operator skill, Hermes Desktop surfaces, and systemd deployment units.

Automatic remote Agency profile installation and complete locate-or-place orchestration are **not yet current Fleet operations**. Fleet has the read-only identity, presence, source-validation, and candidate-selection foundations without pretending the privileged mutation path is already shipped.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| **Hermes Fleet** | Friendly node identity, Fleet authorization, exact-node selection, request envelopes, dispatch, readiness, observed profile presence, execution binding, operator tools, and presentation. |
| **Hermes Keryx** | Authenticated peer identity, discovery, routing, durable task/result state, claims, leases, result delivery, cancellation records, artifacts, and relay behavior. |
| **Hermes Nodescale** | Provider-device identity, explicit device trust, Keryx identity binding, managed membership, and desired managed Fleet state. |
| **Hermes Agent** | Local agent execution, profile installation, models, tools, skills, permissions, memory, and sessions. |
| **Hermes Agency** | Versioned professional Hermes profile distributions, role definitions, bundled skills, and package/catalog metadata. |
| **Headscale / Tailscale** | Private network membership and reachability underneath Nodescale-managed device workflows. |

A useful trust rule is:

```text
connected
!= trusted
!= Keryx-bound
!= Fleet-authorized
!= scheduler-ready
!= exact-profile-present
!= permission for arbitrary execution
```

Mesh membership, Keryx authentication, Fleet authorization, scheduler readiness, profile presence, and Hermes execution are separate gates. No hostname, tag, peer-produced response field, managed role, or same-name profile automatically grants `fleet.hermes.run`.

## Operations

| Operation | Hermes run | Purpose |
| --- | ---: | --- |
| `fleet.health` | No | Bounded Fleet, Keryx, and local Hermes capability health. |
| `fleet.inventory` | No | Safe node identity, version, capability, readiness, and observed-profile summary where available. |
| `fleet.message` | No | Bounded text communication with deterministic acknowledgement. |
| `fleet.hermes.run` | Yes | Deliberately start and observe one authenticated local Hermes run. |

All peer-originated content is treated as untrusted data even when Keryx authenticated the sender. Authentication establishes who sent a response, not whether its contents are safe to trust as local configuration or authority.

## Profile-aware placement foundation

Fleet now observes installed Hermes profile distributions on managed nodes and can distinguish general presence from exact Agency package identity.

For exact identity, the strongest current tuple is:

```text
profile name + distribution version + Agency V1 content digest
```

Fleet can query ready nodes already carrying a requested profile, require an exact digest when needed, validate a profile package from an approved Agency repository at an exact pinned revision, and return the currently ready admitted nodes that could be considered as placement targets.

The current placement-candidate query deliberately does **not** rank or choose a winner, and Fleet does not yet expose the privileged remote install operation required to complete automatic locate-or-place.

See [Profile identity and placement](docs/profile-placement.md).

## Implementation

Hermes Fleet currently contains both the proven Python plugin/runtime and the growing Rust implementation under the same product and repository.

The Python implementation remains the operational integration surface and behavioral compatibility reference. The Rust workspace currently provides durable domain, state, readiness, profile-presence, and control foundations and is intended to assume more of the permanent runtime over time without changing the product identity or external contracts.

Repository layout:

```text
hermes_fleet/           Python Fleet plugin/runtime and integration surfaces
crates/fleet-domain/    Rust domain types, authorization, observations, readiness
crates/fleet-state/     Rust durable Fleet state and state queries
crates/fleet-control/   Rust local managed-state/observation control service
desktop/                Hermes Desktop Fleet plugin
fixtures/               language-neutral compatibility fixtures
ops/                    service and deployment assets
docs/                   ecosystem, architecture, operations, and contract docs
AGENTS.md                coding-agent architecture and repository guidance
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

Coding agents should start with [`AGENTS.md`](AGENTS.md) before changing cross-repository contracts.

## Documentation

Start with the [documentation index](docs/README.md).

- [Ecosystem map](docs/ecosystem.md)
- [Architecture](docs/architecture.md)
- [Profile identity and placement](docs/profile-placement.md)
- [Deployment](docs/deployment.md)
- [Managed projection V1](docs/managed-projection-v1.md)
- [Node observations and scheduler readiness](docs/node-readiness.md)
- [Fleet Desktop](docs/desktop.md)
- [Fleet Canvas topology](docs/canvas.md)
- [Integration verification](docs/smoke-test.md)
- [Coding-agent guide](AGENTS.md)
- [Operator skill](SKILL.md)
- [Changelog](CHANGELOG.md)

Implementation chronology, checkpoint hashes, machine-specific rollout notes, and agent plans belong in pull requests, issues, releases, CI artifacts, or local workspace state rather than durable product documentation.

## Known limitations

- Automatic remote Agency profile installation and complete locate-or-place orchestration are not yet part of the current Fleet operation surface.
- Cross-node running-task cancellation is not yet proven end to end and therefore remains unavailable.
- Keryx relay offline mailbox durability is a Keryx concern; Fleet does not add a second mailbox or queue.
- Cross-node artifact transfer, fan-out, pub/sub, broadcast, persistent inboxes, multi-node chat, durable or executable workflow graphs, and multi-tenant control are outside the current Fleet surface. The Desktop includes only a local non-executing workflow-editor foundation.
- Disposable per-task containers and recipe-based execution environments are architectural direction, not a current Fleet contract.
- A running Hermes gateway must be restarted after a plugin update before that process sees newly installed Fleet model tools.

## License

Hermes Fleet is licensed under the GNU Affero General Public License v3.0 only (`AGPL-3.0-only`). See [LICENSE](LICENSE).

Code published in earlier commits under a different license remains available under the terms that applied when it was published.
