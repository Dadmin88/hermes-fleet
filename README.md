# Hermes Fleet

Hermes Fleet is the coordination layer for a group of Hermes-capable machines.

Its job is simple to describe:

> Know which machines are available, know what they are allowed to do, and send the right work to the right machine.

Hermes Fleet does not replace Hermes Agent, Keryx, or Nodescale. It sits above those systems and coordinates them.

## How it fits into the Hermes stack

```text
Nodescale
  Who is this device, and is it trusted?
        ↓
Keryx
  Which application peer is actually speaking, and how does work travel?
        ↓
Hermes Fleet
  What may this node do, and where should work run?
        ↓
Hermes Agent
  Perform the actual work.
```

A network connection is not enough to grant execution rights. A trusted device is not automatically allowed to run every Fleet operation.

## What Fleet does today

The current product supports four operations:

| Operation | What it does | Starts Hermes work? |
| --- | --- | ---: |
| `fleet.health` | Checks a node's Fleet/Keryx/Hermes capability health. | No |
| `fleet.inventory` | Returns a small, safe summary of node identity and capabilities. | No |
| `fleet.message` | Sends bounded text to one exact node. | No |
| `fleet.hermes.run` | Deliberately starts one Hermes run on an authorized node. | Yes |

Only `fleet.hermes.run` is an execution operation.

Fleet also supports the accepted Nodescale managed-projection contract. A trusted, Keryx-bound Nodescale device can be projected into Fleet with safe baseline authority:

```text
fleet.health
fleet.inventory
fleet.message
```

Managed projection never grants `fleet.hermes.run` automatically. Local Fleet deny rules remain authoritative.

## Why Keryx exists underneath Fleet

Fleet does not implement its own network transport or task ledger.

Keryx owns:

- authenticated peer identity;
- relay routing and delivery;
- durable task and result state;
- claims, leases, deadlines, and retries;
- result delivery and artifact descriptors;
- reconnect and offline-mailbox behavior.

Fleet uses Keryx rather than duplicating those responsibilities.

## Duplicate-safe Hermes execution

Starting remote Hermes work has one important failure case: a process can crash after a Hermes run starts but before Fleet records the result.

Fleet keeps a small execution-binding record so a reclaimed Keryx task does not accidentally start a second Hermes run.

The important states are:

```text
creating
running
completed
indeterminate
```

If Fleet knows the Hermes run ID, it can resume watching that run. If it already has the terminal result, it can replay that result to Keryx. If it cannot prove whether a run was created, it fails closed instead of guessing.

## Python and Rust implementations

Hermes Fleet is one product with two implementation stages in this repository.

- **Python implementation:** the proven reference implementation and current production prototype.
- **Rust implementation:** the permanent implementation now being built against the same behavior and shared fixtures.

There is no separate "Fleet-RS" product.

The first Rust foundation on `main` contains the real `fleet-domain` crate and language-neutral compatibility fixtures for operation vocabulary, exact-node selection, managed projection decisions, local-deny behavior, and execution-recovery decisions.

Storage, transport, scheduling, profile distribution, Fleet Sentinel, and the final UI are separate later steps.

## Install as a Hermes plugin

Hermes Fleet can be installed as a Git-backed Hermes plugin:

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
```

If a Hermes gateway is already running, restart it after installing or updating the plugin so it loads the new tools:

```bash
hermes gateway restart
```

`hermes fleet init` creates missing Fleet state without overwriting valid operator-managed state.

## Operator commands

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

Model tools:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

Cross-node cancellation currently fails closed because Fleet cannot yet prove that a remote worker observed the cancellation and stopped its bound Hermes run.

## Security model

Fleet keeps authentication, authorization, and returned content separate.

- Keryx proves which peer sent a request.
- Fleet decides whether that peer may perform the requested operation.
- Nodescale-managed baseline authority is limited to health, inventory, and message.
- Local Fleet deny rules always win over generated authority.
- Peer-produced text and JSON are treated as untrusted content.
- Exact node selection is required. Fleet does not silently execute against a fuzzy name match.

## What Fleet is not

Fleet is not:

- a second Keryx relay;
- a replacement for Hermes Agent;
- a general remote shell;
- a scheduler yet;
- a Kubernetes replacement;
- a shared database between Nodescale, Keryx, and Fleet.

Each system owns its own state.

## Where the project is going

The roadmap is building toward:

```text
trusted managed nodes
→ durable Fleet state
→ Rust service/control plane
→ Keryx communication and Hermes execution parity
→ CPU/RAM/GPU inventory and readiness
→ content-addressed Hermes profiles
→ reservations and scheduling
→ quarantine and Fleet Sentinel
→ frontend/plugin UI
```

The goal is a private pool of independently owned machines that can join safely, advertise what they can do, receive authorized work, and return results through one coherent Fleet experience.

## Documentation

- [Architecture](docs/architecture.md)
- [Managed projection V1](docs/managed-projection-v1.md)
- [Deployment](docs/deployment.md)
- [Smoke testing](docs/smoke-test.md)
- [Operator skill](SKILL.md)

## License

Current versions of Hermes Fleet are licensed under the GNU Affero General Public License v3.0 only (`AGPL-3.0-only`). See [LICENSE](LICENSE).

Code published in earlier commits under the MIT License remains available under the license terms that applied when it was published.
