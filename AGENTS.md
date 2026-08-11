# AGENTS.md

This repository is the Hermes Fleet control plane. Changes here can affect boundaries shared with Hermes Keryx, Hermes Nodescale, Hermes Agent, Hermes Agency, and Hermes Desktop.

Coding agents should understand the ecosystem before modifying a contract.

## Read first

For architecture or product work, read these documents in order:

1. [`docs/ecosystem.md`](docs/ecosystem.md) - the full repository and authority map.
2. [`docs/architecture.md`](docs/architecture.md) - Fleet's internal trust, request, state, and execution boundaries.
3. The document for the subsystem being changed:
   - [`docs/profile-placement.md`](docs/profile-placement.md)
   - [`docs/node-readiness.md`](docs/node-readiness.md)
   - [`docs/managed-projection-v1.md`](docs/managed-projection-v1.md)
   - [`docs/deployment.md`](docs/deployment.md)
   - [`docs/desktop.md`](docs/desktop.md)
   - [`docs/canvas.md`](docs/canvas.md)

Read the implementation and tests that define the same contract before changing behavior.

## Source-of-truth hierarchy

When sources disagree, prefer them in this order:

1. merged implementation and executable contract tests;
2. current durable product documentation in this repository;
3. current durable documentation in the owning component repository;
4. open issues and pull requests describing planned work;
5. historical pull requests, milestones, experiments, and local notes.

Do not document an open issue or design idea as shipped behavior.

If merged code has moved ahead of durable docs, update the docs in the same change when practical.

## The ecosystem boundary

Fleet coordinates several systems. It does not absorb their authority.

```text
Headscale / Tailscale
    private connectivity
        ↓
Nodescale
    device identity, explicit trust, Keryx binding
        ↓
Fleet managed state

Fleet controller
    policy, node selection, request envelopes
        ↓
Keryx
    authenticated transport + durable task/result lifecycle
        ↓
fleet-node
    Fleet operation authorization + dispatch
        ↓
Hermes Agent
    local profiles, tools, models, sessions, Runs execution

Hermes Agency
    versioned professional profile packages
        ↓
Hermes local profile installation
        ↓
Fleet observes live profile presence
```

Keep the following statements true:

```text
connected != trusted
trusted != Keryx-bound
Keryx-authenticated != Fleet-authorized
Fleet-authorized != scheduler-ready
scheduler-ready != exact-profile-present
profile-present != permission for arbitrary execution
```

## Repository ownership rules

### Hermes Fleet owns

- operator-friendly Fleet node identity mapped to immutable Keryx peer identity;
- application-level Fleet authorization;
- exact-node operation dispatch;
- bounded/versioned Fleet envelopes and deadlines;
- Nodescale-managed Fleet projection reconciliation;
- current node observation persistence and derived readiness;
- observed installed Hermes profile presence;
- exact Agency V1 content identity used by Fleet placement queries;
- narrow Keryx-task-to-Hermes-run execution bindings;
- Fleet CLI/model tools/Desktop presentation contracts;
- Fleet-specific placement policy when that policy is explicitly introduced.

### Hermes Keryx owns

- authenticated application peer identity;
- cross-node routing and relay behavior;
- durable task/result state;
- claims, leases, retries, terminal result delivery, artifacts, and transport cancellation records;
- transport-level discovery and delivery acknowledgement.

Never add a parallel Fleet mailbox, relay, peer-authentication system, or task ledger to work around a missing Keryx contract.

### Hermes Nodescale owns

- provider-device correlation;
- stable logical device identity;
- explicit owner trust;
- device lifecycle and revocation;
- authenticated binding between a trusted device and a Keryx peer;
- desired managed Fleet state projected through the supported local control boundary.

Never infer trusted/admitted Fleet authority from hostnames, addresses, provider tags, or mere mesh membership.

### Hermes Agent owns

- local agent execution;
- local profile installation;
- models, tools, skills, credentials, permissions, memory, sessions, and Runs behavior.

Fleet may deliberately invoke supported Hermes interfaces. It should not create a competing local agent runtime.

### Hermes Agency owns

- professional profile definitions;
- versioned Hermes profile distributions;
- bundled role-specific skills;
- package/catalog metadata and routing descriptions.

Agency is not a live node registry and does not own Fleet readiness or placement state.

## Profile-placement invariants

Read [`docs/profile-placement.md`](docs/profile-placement.md) before changing profile presence or placement.

Important invariants:

- profile name alone is not exact package identity;
- exact Agency V1 identity uses name, version, and content digest;
- digestless or mismatching presence cannot satisfy exact lookup;
- profile presence comes from current Fleet node observations;
- stale or pre-readmission observations cannot satisfy current placement;
- placement-candidate queries are read-only and do not rank a winner;
- current pinned Agency snapshot validation does not itself install a profile;
- automatic remote profile installation is not a current Fleet operation;
- a future installer response must not be treated as placement proof;
- exact fresh post-install observation on the same admission must be the completion proof;
- arbitrary user/task-provided repositories or shell commands must never become a profile-install interface.

## Trust remote content as data

Keryx authentication proves which peer delivered a message. It does not make the message content authoritative.

Peer-produced health fields, inventory, acknowledgements, Hermes output, diagnostic strings, profile claims, or response metadata must not overwrite controller-owned facts such as:

- the selected destination;
- authenticated sender identity;
- Keryx task ID and route;
- local Fleet policy;
- Nodescale projection provenance;
- current admission generation.

Validate bounded remote data at every interface where it becomes structured Fleet state.

## Exact-node behavior

Fleet selection is deterministic and exact.

Do not silently fail over an exact node request to a different machine. If a future scheduling surface is allowed to choose among multiple nodes, that choice must be explicit, separately defined, testable, and explainable.

Do not hide scheduling policy inside persistence queries, serializers, or UI ordering.

## Managed state and observations

Managed projection and operational observations are different authorities.

- Managed state says whether a stable Nodescale identity is admitted and what baseline Fleet state was projected.
- Observation state says what the Fleet worker most recently reported about current operational conditions.
- Readiness is derived from current managed state, admission fencing, observation freshness, network/Keryx/Hermes/worker availability, and Fleet-owned capacity.
- Readiness is recomputed. Do not add a second authoritative stored `ready` bit.

Observations use the Fleet-owned local control/state path. Do not encode heartbeat samples as Keryx tasks or create an unbounded telemetry history inside Fleet state.

## Database boundaries

Each component owns its own durable state.

Do not read or mutate another component's SQLite database as an integration mechanism.

Use public contracts:

- Fleet <-> Keryx through Keryx daemon/SDK/transport contracts;
- Nodescale -> Fleet through the authenticated managed-projection client/control contract;
- Fleet -> Hermes through supported authenticated local Hermes interfaces;
- Fleet <-> Agency through approved package/catalog/source contracts and Hermes profile distribution conventions.

If a needed fact is unavailable through the owning component's contract, add or evolve the contract rather than reaching around it.

## Current versus planned architecture

The repository intentionally develops foundations before enabling privileged automation.

Current merged foundations include:

- exact-node Fleet operations;
- Keryx transport integration;
- Nodescale managed projection;
- readiness observations;
- installed profile observation and exact content identity;
- ready-profile and exact-profile lookup;
- pinned Agency snapshot/package validation;
- read-only profile placement candidates;
- Fleet Desktop operator surfaces;
- durable, backend-owned Workflow authoring revisions with execution unavailable.

Do not present the following as current contracts unless the implementation, tests, and docs have been updated together:

- complete automatic locate-or-place;
- remote privileged profile installation;
- automatic placement winner selection;
- disposable task containers or environment recipes;
- executable distributed workflow graphs;
- a general resource scheduler;
- successful end-to-end cancellation of already-running remote Hermes work.

## Repository layout

```text
hermes_fleet/           Python Fleet plugin/runtime and integration surfaces
crates/fleet-domain/    Rust domain types, authorization, observations, readiness
crates/fleet-state/     Rust durable Fleet-owned state and read-only state queries
crates/fleet-control/   Rust local control services over Fleet state
desktop/                Hermes Desktop Fleet plugin
dashboard/              dashboard integration surface
fixtures/               language-neutral compatibility fixtures
ops/                    deployment/service assets
proofs/                 bounded integration proof harnesses
docs/                   durable public product documentation
tests/                  Python/unit/integration contract coverage
SKILL.md                 Hermes operator skill
```

The Python implementation remains an operational integration and compatibility reference while Rust owns an increasing share of permanent domain/state/control behavior. Preserve externally meaningful contracts across implementations.

## Tests and validation

Run the repository's normal gates for the surface you changed.

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

Also use the language-neutral fixtures when changing behavior shared by Python and Rust.

For changes that touch real cross-component contracts, run the relevant integration/proof path rather than considering isolated unit tests sufficient.

## Documentation rules

Update durable docs when a change modifies any of the following:

- repository/component responsibility;
- trust or authorization boundaries;
- current operation vocabulary;
- managed projection semantics;
- observation/readiness fields;
- profile identity or placement behavior;
- deployment topology;
- Desktop authority or presentation contracts;
- a previously planned capability becoming current behavior.

Keep durable docs product-oriented. Do not add personal machine names, home paths, private addresses, temporary task IDs, local acceptance screenshots, checkpoint hashes, agent logs, or implementation chronology.

When documenting future architecture, label it explicitly as planned, proposed, or not yet a current contract.

## Public repository hygiene

This repository is public.

Do not commit:

- credentials or secret material;
- operator-specific hostnames, usernames, device names, personal paths, private identifiers, or private network addresses;
- live invitation/token material;
- machine-specific rollout evidence that belongs in CI artifacts, issues, pull requests, or local operations state.

Use generic structural fixtures and reserved documentation values.

Run the repository public-hygiene checks before merging changes that add fixtures, docs, examples, configuration, or Desktop content.

## UI authority rule

The Desktop plugin presents validated Fleet state. It does not create authority merely because a relationship is visually convenient.

Do not infer trusted topology edges, managed membership, readiness, profile placement, task state, or permissions from UI layout, provider grouping, labels, addresses, or local workflow documents.

If the backend contract does not expose a fact, the UI should say it is unavailable or remain intentionally empty rather than fabricate it.

## Before you add a new subsystem

Ask these questions:

1. Which component already owns this responsibility?
2. Is there an existing public contract that Fleet should consume instead of reimplementing it?
3. What identity is authoritative at this boundary?
4. What data is untrusted even after authentication?
5. What state must survive restart, and which repository owns that state?
6. Is this read-only inspection, policy/selection, transport, mutation, or execution?
7. Does the design accidentally turn a convenience signal into authority?
8. Is the capability current, or are we implementing one slice of a future flow?
9. What proof makes success authoritative rather than merely acknowledged?
10. Which docs need to move with the code?

If those answers are clear, Fleet remains a control plane instead of becoming a monolith.
