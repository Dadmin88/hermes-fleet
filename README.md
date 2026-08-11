# Hermes Fleet

> **The control plane for distributed Hermes.**
>
> Hermes Fleet turns a collection of Hermes-capable machines into an operator-controlled fleet: one place to understand the network, address exact nodes, enforce Fleet policy, inspect readiness, discover professional profiles, and deliberately run Hermes work across machines.

[![CI](https://github.com/Dadmin88/hermes-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/Dadmin88/hermes-fleet/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

Hermes already knows how to run an agent on one machine. The surrounding Hermes ecosystem adds private networking, trusted device membership, authenticated transport, professional profiles, and operator UI.

**Fleet is the layer that makes those pieces act like one system without collapsing their responsibilities into one giant service.**

If you want the full architecture first, start with the **[Hermes Fleet ecosystem map](docs/ecosystem.md)**.

---

## Why Fleet exists

A distributed Hermes setup has several different questions to answer:

- Which devices belong to the network?
- Which devices have actually been trusted?
- Which authenticated application peer belongs to each device?
- Which machine should an operator address?
- Is that machine alive and ready for another Fleet-owned run?
- Which Hermes professional profiles are installed there?
- Is the requested Fleet operation allowed?
- How does work move to that machine and return durably?
- What actually starts and executes the local agent run?

Those are **not the same question**, and they should not have the same source of truth.

Fleet sits in the middle and coordinates the answers.

A useful high-level analogy is **a control plane for Hermes workers**. Fleet has some of the same concerns people associate with cluster orchestrators: node inventory, placement facts, readiness, policy, dispatch, and observability. Fleet is intentionally not a second network, not a second task transport, and not its own container runtime. Planned execution-fabric work is designed to orchestrate mature runtime backends rather than reimplement them.

## The ecosystem

```mermaid
flowchart TB
    operator["Operator / calling agent"]
    surfaces["Fleet surfaces<br/>Desktop · CLI · Hermes model tools"]
    fleet["Hermes Fleet<br/>control · policy · readiness · selection"]

    network["Headscale / Tailscale<br/>private connectivity"]
    nodescale["Hermes Nodescale<br/>device identity · trust · admission"]
    keryx["Hermes Keryx<br/>authenticated transport · durable tasks/results"]
    worker["fleet-node<br/>operation validation · dispatch"]
    hermes["Hermes Agent<br/>models · tools · profiles · sessions · Runs"]
    agency["Hermes Agency<br/>professional profile distributions"]

    operator --> surfaces --> fleet
    network --> nodescale
    nodescale -->|"managed state"| fleet
    fleet -->|"bounded Fleet request"| keryx
    keryx -->|"authenticated delivery"| worker
    worker -->|"explicit execution operation"| hermes
    hermes --> worker
    worker -->|"durable result"| keryx
    keryx --> fleet

    agency -->|"profile packages"| hermes
    worker -. "readiness + installed profile observations" .-> fleet
    agency -. "catalog + exact package identity" .-> fleet
```

### What each project owns

| Project | Owns | Fleet uses it for |
| --- | --- | --- |
| **Headscale / Tailscale** | Private network membership and reachability | Getting machines onto a private network |
| **[Hermes Nodescale](https://github.com/Dadmin88/hermes-nodescale)** | Device identity, explicit trust, lifecycle, Keryx identity binding, managed Fleet projection | Knowing which devices are admitted and which Keryx peer belongs to them |
| **[Hermes Keryx](https://github.com/Dadmin88/hermes-keryx)** | Authenticated peer identity, routing, durable tasks/results, claims, leases, relay delivery, artifacts | Moving Fleet work and results between machines |
| **Hermes Fleet** | Fleet authorization, exact-node addressing, readiness, dispatch, profile presence, placement facts, operator surfaces | Coordinating the distributed system |
| **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | Local models, tools, skills, profiles, permissions, memory, sessions, Runs execution | Actually doing the AI work on a machine |
| **[Hermes Agency](https://github.com/Dadmin88/hermes-agency)** | Versioned professional Hermes profile distributions and bundled skills | Defining the professional worker packages Fleet can identify and observe |
| **Hermes Desktop + Fleet plugin** | Human-facing Fleet presentation and bounded operator actions | Seeing and operating the fleet visually |

The architectural rule is simple:

> **Fleet coordinates these systems through their contracts. It does not reimplement them.**

Keryx remains the transport. Nodescale remains the device-trust authority. Hermes remains the local execution runtime. Agency remains the professional profile source.

---

## The trust chain

Fleet is deliberately built around separate gates:

```text
network reachable
    != device trusted
    != Keryx identity bound
    != Fleet operation authorized
    != node scheduler-ready
    != exact requested profile present
    != permission for arbitrary execution
```

That distinction matters even on a private network.

A machine being connected does not make it trusted. A Keryx-authenticated peer is not automatically authorized for every Fleet operation. A node being scheduler-ready does not grant `fleet.hermes.run`. A same-name profile does not prove that the expected Agency package is installed.

Fleet fails closed rather than promoting convenient signals into authority.

---

## What Fleet does today

Fleet currently provides:

- **Stable operator-facing node identity** mapped to immutable Keryx peer identities.
- **Deterministic exact-node selection** for operator-directed work.
- **Bounded, versioned Fleet request envelopes** with absolute deadlines and payload limits.
- **Default-deny per-node authorization** for Fleet operations.
- **Direct node operations** for health, inventory, and bounded messaging.
- **Deliberate remote Hermes execution** through the explicit `fleet.hermes.run` operation.
- **Durable task-to-run binding** so reclaimed Keryx work does not silently create duplicate Hermes runs.
- **Durable status reattachment** through Keryx task/result state.
- **Nodescale-managed Fleet projection** with Fleet-local deny precedence.
- **Current node observations** with freshness, worker capacity, resource facts, and explainable scheduler readiness.
- **Installed Hermes profile observation** as part of current node state.
- **Exact Agency V1 profile content identity** when Fleet can safely prove it.
- **Ready-node lookup by profile**, including exact `{name, version, content_digest}` lookup.
- **Pinned immutable Hermes Agency package validation** at an exact repository revision.
- **Read-only placement-candidate discovery** for eligible scheduler-ready nodes.
- **CLI, Hermes model tools, an operator skill, Hermes Desktop integration, and deployment assets.**

### What Fleet does not pretend to do yet

These are not current Fleet contracts:

- runtime-neutral Fleet Recipe execution;
- automatic placement winner selection;
- disposable per-task worker environments;
- Docker, PRoot/OCI, or other execution backends as Fleet runtime contracts;
- a general CPU/GPU workload scheduler;
- executable distributed workflow graphs;
- persistent automatic remote Agency profile installation;
- a second mailbox, relay, or transport system;
- proven end-to-end cancellation of an already-running remote Hermes run.

The planned execution-fabric direction is to make agent/environment requirements portable through validated Fleet Recipes and materialize them on compatible trusted nodes using mature backend runtimes. Persistent host profile installation is not the default planned way to make a professional agent available remotely. Planned capabilities become product claims only after their contracts, implementation, tests, and operational proofs are merged.

---

## What happens when you run work remotely?

A normal exact-node Fleet request looks like this:

```mermaid
sequenceDiagram
    participant U as Operator / calling agent
    participant F as Fleet controller
    participant K1 as Origin keryxd
    participant K as Keryx routing / relay
    participant K2 as Destination keryxd
    participant N as fleet-node
    participant H as Hermes Runs API

    U->>F: fleet operation for an exact node
    F->>F: resolve target + build bounded request
    F->>K1: submit through Keryx
    K1->>K: authenticated durable delivery
    K->>K2: deliver to exact peer
    K2->>N: authenticated sender + task context
    N->>N: validate destination, deadline, policy, operation

    alt Direct Fleet operation
        N-->>K2: bounded Fleet result
    else fleet.hermes.run
        N->>H: start and observe local Hermes run
        H-->>N: run status / output
        N-->>K2: bounded terminal result
    end

    K2-->>K: durable result
    K-->>K1: authenticated result delivery
    K1-->>F: task result / reattachment
    F-->>U: result
```

A direct Fleet request never needs to become an agent run. `fleet.health`, `fleet.inventory`, and `fleet.message` stay inside Fleet.

Only the explicit executable operation enters Hermes.

Fleet also does not silently retarget an exact-node request to another machine when the requested destination is unavailable.

---

## Nodes, readiness, and capacity

A node can be known to Fleet without being usable for work.

Fleet derives scheduler readiness from separate facts:

1. Nodescale-managed identity is currently active.
2. The latest Fleet observation is fresh.
3. Required network/control reachability is observed.
4. Keryx is available.
5. Hermes is available.
6. The Fleet worker is available.
7. At least one Fleet-owned execution slot remains.

That means a node can legitimately be:

- managed but offline;
- alive but missing Hermes;
- healthy but saturated;
- carrying a useful profile but stale;
- scheduler-ready but not carrying the requested profile.

Readiness is **derived**, not stored as a magical `ready=true` authority bit.

See **[Node observations and scheduler readiness](docs/node-readiness.md)**.

---

## Professional profiles and Hermes Agency

[Hermes Agency](https://github.com/Dadmin88/hermes-agency) supplies professional Hermes profile distributions: engineers, designers, researchers, reviewers, operators, writers, and other specialized roles with bundled skills.

For the current native execution path, Hermes installs and runs those profiles locally. Fleet can answer the distributed observation question:

> **Which admitted, ready machines currently report the exact profile package I need?**

Fleet can observe installed profile distributions and, for supported Agency packages, prove an exact content identity:

```text
profile name
+ distribution version
+ hermes-agency-profile-content.v1 SHA-256 digest
```

That lets Fleet distinguish:

- the exact approved package;
- the same profile name with different content;
- a legacy or generic installation without a provable exact Agency digest;
- no current installation of that profile at all.

### Native presence versus planned Recipe materialization

Fleet already has read-only foundations for exact native-profile locality:

```text
requested Agency package
        ↓
find exact ready native carriers
        ↓
inspect other eligible ready candidates
```

Those facts remain useful, but the older plan to complete availability by persistently installing a missing profile onto the destination host is superseded as the default architectural direction.

The planned execution fabric instead treats the exact Agency package as an input to a runtime-neutral Fleet Recipe. Fleet resolves the Recipe, chooses a trusted node whose platform/backend capabilities satisfy it, and materializes a fresh worker environment using that node's supported backend. A compatible node therefore does not need the professional profile permanently installed on the host beforehand.

Recipe execution, heterogeneous execution backends, automatic scheduling, and environment materialization are **planned architecture, not shipped Fleet behavior**.

See **[Profile identity, presence, and execution locality](docs/profile-placement.md)** for the precise current and planned boundary.

---

## Fleet Desktop

Fleet includes a Hermes Desktop integration for operating the system visually.

The Desktop surface is intentionally backed by validated Fleet state rather than frontend guesses. It can present managed and observed machines, readiness evidence, worker capacity, resources, profile presence, operator-facing topology, and supported actions without turning UI layout into authority.

Provider-visible machines remain visibly different from trusted/admitted managed machines.

The local Workflow editor is currently an authoring surface only. A line drawn in the UI is not a durable distributed execution graph.

See **[Fleet Desktop](docs/desktop.md)** and **[Fleet Canvas topology](docs/canvas.md)**.

---

## Install Fleet as a Hermes plugin

Fleet is a Hermes plugin. This quick start assumes Git is authorized for the repository and that the supporting Keryx/runtime services required by your deployment are available.

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

If a Hermes gateway is already running, restart it after installing or updating Fleet so the process loads the current Fleet tools:

```bash
hermes gateway restart
```

`hermes fleet init` creates missing Fleet state without overwriting valid operator-managed state.

For a real multi-machine deployment, follow **[Deployment](docs/deployment.md)** rather than treating plugin installation as the entire stack.

---

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

Examples:

```bash
# Inspect the fleet
hermes fleet list
hermes fleet show compute-a

# Check a node
hermes fleet health compute-a
hermes fleet inventory compute-a

# Send a bounded Fleet message
hermes fleet message compute-a "ready for the next task?"

# Deliberately start Hermes work on that exact node
hermes fleet run compute-a "Review the current branch and summarize the risks."
```

Cross-node running-task cancellation currently fails closed because Fleet cannot yet prove that the destination worker observed cancellation and stopped an already-bound Hermes run.

### Hermes model tools

Fleet also exposes model-callable tools:

- `fleet_list_nodes`
- `fleet_get_node`
- `fleet_get_health`
- `fleet_send_message`
- `fleet_run`
- `fleet_get_task`
- `fleet_cancel_task`

The same authorization and trust boundaries apply whether a human or another agent initiated the operation.

---

## Fleet operation vocabulary

| Operation | Starts Hermes? | Purpose |
| --- | ---: | --- |
| `fleet.health` | No | Bounded Fleet, Keryx, and local Hermes capability health |
| `fleet.inventory` | No | Safe node identity, capability, readiness, resource, and observed-profile summary where available |
| `fleet.message` | No | Bounded text communication with deterministic acknowledgement |
| `fleet.hermes.run` | **Yes** | Deliberately start and observe one authenticated local Hermes run |

Receiving a Fleet message never implies permission to start Hermes.

Peer-originated content remains untrusted data even when Keryx authenticated the sender. Authentication proves **who delivered it**, not that returned text, fields, or model-generated content should become local authority.

---

## Repository layout

Fleet currently includes both the proven Python plugin/runtime and a growing Rust implementation under one product contract.

```text
hermes_fleet/           Python Fleet plugin/runtime and integration surfaces
crates/fleet-domain/    Rust domain types, authorization, observations, readiness
crates/fleet-state/     Rust durable Fleet-owned state and placement/read queries
crates/fleet-control/   Rust local managed-state / observation control service
desktop/                Hermes Desktop Fleet plugin
dashboard/              dashboard integration surface
fixtures/               language-neutral compatibility fixtures
ops/                    service and deployment assets
proofs/                 bounded cross-component proof harnesses
docs/                   durable public documentation
tests/                  Python/unit/integration contract coverage
AGENTS.md                coding-agent architecture and repository guidance
SKILL.md                 Hermes operator skill
```

The Python implementation remains an operational integration and compatibility reference while Rust owns an increasing share of the permanent domain, state, and control foundation.

Externally meaningful contracts matter more than which language currently implements them.

---

## Documentation map

New to Fleet? Read these in order:

| Document | Use it when... |
| --- | --- |
| **[Ecosystem map](docs/ecosystem.md)** | You want to understand the whole Hermes system and how the repositories fit together |
| **[Architecture](docs/architecture.md)** | You need Fleet's internal trust, state, request, and execution boundaries |
| **[Profile identity and locality](docs/profile-placement.md)** | You are working with Hermes Agency profiles, exact package identity, native presence, or future Recipe placement |
| **[Node readiness](docs/node-readiness.md)** | You need to understand liveness, freshness, capacity, resources, or scheduler readiness |
| **[Managed projection V1](docs/managed-projection-v1.md)** | You are integrating Nodescale-managed device state into Fleet |
| **[Deployment](docs/deployment.md)** | You are setting up controller, worker, Keryx, and Hermes services |
| **[Fleet Desktop](docs/desktop.md)** | You are operating or developing the Desktop integration |
| **[Fleet Canvas](docs/canvas.md)** | You are working on topology or the local workflow authoring surface |
| **[Integration verification](docs/smoke-test.md)** | You need repeatable real two-node verification |
| **[AGENTS.md](AGENTS.md)** | You are a coding agent or maintainer changing cross-repository contracts |
| **[SKILL.md](SKILL.md)** | You are operating Fleet from Hermes |
| **[CHANGELOG.md](CHANGELOG.md)** | You need release history |

The complete documentation index is in **[docs/README.md](docs/README.md)**.

---

## Development

Python:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Rust:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --workspace
```

Use the language-neutral fixtures when changing behavior shared by Python and Rust.

For changes that cross repository boundaries, run the relevant real integration/proof path rather than considering isolated unit tests sufficient.

Coding agents should read **[AGENTS.md](AGENTS.md)** before changing architecture or cross-component contracts.

---

## Design principles

Fleet tries to preserve a few boring-but-powerful rules:

- **Exact identities over convenient names.** Friendly names are presentation; immutable identities carry authority.
- **Explicit trust over network proximity.** Connected is not trusted.
- **Authorization stays local to the application boundary.** Transport authentication is not Fleet permission.
- **Readiness is evidence, not optimism.** Missing or stale facts fail closed.
- **No duplicate infrastructure for convenience.** Keryx owns transport and durable task state.
- **Orchestrate mature runtime primitives instead of recreating them.** Planned execution backends should wrap proven infrastructure rather than turn Fleet into a homegrown runtime.
- **Do not trust acknowledgements as proof of side effects.** Durable observed state should prove important mutations.
- **Current product and future architecture are documented separately.** A roadmap idea is not a shipped capability.

Those rules are what keep Fleet a control plane instead of letting it slowly become a distributed monolith.

---

## License

Hermes Fleet is licensed under the GNU Affero General Public License v3.0 only (`AGPL-3.0-only`). See [LICENSE](LICENSE).

Code published in earlier commits under a different license remains available under the terms that applied when it was published.