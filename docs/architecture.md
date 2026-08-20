# Hermes Fleet Architecture

Hermes Fleet is an application-level coordination layer built on Hermes Keryx. Keryx supplies authenticated peer transport and durable task/result delivery; Fleet adds stable operator identity, exact-node selection, authorization, operation dispatch, managed-state reconciliation, current readiness, profile/locality facts, and the narrow state required to coordinate deliberate Hermes execution.

Fleet does not implement a parallel relay, task ledger, workflow engine, mailbox, or container runtime.

For the frozen **vNext planned architecture**, including persistent Agent Instances, immutable RunAuthority, temporary Run Capsules, disposable execution bodies, Templar, Vault, and the local-vs-remote machine-boundary invariant, read [vNext foundation](vnext-foundation.md) first. Current merged behavior described in this document remains current truth until each vNext phase is implemented and proven.

For the cross-repository view before reading Fleet internals, see [Ecosystem map](ecosystem.md).

## Authority model

Four systems form the authority chain for managed remote execution:

1. **Nodescale** owns managed device membership, device trust, Keryx identity binding, and desired managed Fleet state.
2. **Keryx** owns authenticated transport peer identity, routing, task/result durability, claims, leases, artifacts, and relay behavior.
3. **Fleet** owns application-level node identity, local authorization, operation dispatch, managed-state reconciliation, current readiness/placement truth, and execution correlation.
4. **Hermes** owns local agent execution and its models, tools, profiles, skills, credentials, permissions, memory, and sessions.

A fifth ecosystem component, **Hermes Agency**, supplies versioned professional Hermes profile packages and their catalog metadata. Agency contributes capability content, not live trust, transport, scheduling, or execution authority.

These authorities are deliberately not interchangeable.

- A device being present on the private network does not grant Fleet authority.
- A device trusted by Nodescale is not automatically authorized for every Fleet operation.
- A Keryx-authenticated peer is not automatically authorized for every Fleet operation.
- A Fleet role, tag, managed membership record, or scheduler-ready state does not automatically authorize Hermes execution.
- A profile name does not prove exact package content.
- An Agency catalog entry does not prove that a profile is installed on a live node.
- A peer-produced response is still untrusted content even when its sender identity is authenticated.

A useful shorthand is:

```text
connected
!= trusted
!= Keryx-bound
!= Fleet-authorized
!= scheduler-ready
!= exact-profile-present
!= execution permission
```

## Operation model

Fleet exposes a small current versioned operation vocabulary:

| Operation | Class | Creates a Hermes run | Description |
| --- | --- | ---: | --- |
| `fleet.health` | direct | No | Bounded Fleet/Keryx/Hermes capability health. |
| `fleet.inventory` | direct | No | Safe node identity, capability, readiness, and profile-presence summary where available. |
| `fleet.message` | direct | No | Bounded text communication and acknowledgement. |
| `fleet.hermes.run` | executable | Yes | Deliberately start and observe one local Hermes run. |

Every request uses the same bounded Fleet envelope over Keryx. The worker validates the authenticated sender, destination, operation, envelope version, Keryx metadata, absolute deadline, payload limits, and local policy before choosing a handler.

Direct operations never enter the Hermes execution path.

There is currently no shipped `fleet.profile.install` operation. Persistent automatic remote host-profile mutation is outside the current operation vocabulary and is not the default planned mechanism for future distributed profile availability.

## Request flow

A typical current cross-node request follows this path:

```text
Fleet controller
  -> resolve exact node name to Keryx peer ID
  -> build bounded Fleet envelope and deadline
  -> local Keryx daemon
  -> Keryx routing / relay
  -> destination Keryx daemon
  -> fleet-node dispatcher
       -> direct Fleet handler
       -> or deliberate Hermes Runs handler
  -> normal Keryx terminal result
  -> Fleet controller / durable status reattachment
```

The authenticated sender identity comes from Keryx delivery context. A sender field inside JSON is never authoritative.

Fleet preserves Keryx submission facts such as task ID, routed peer, and delivery route instead of reconstructing them from peer-produced response content.

## Exact-node selection

The legacy schema-v1 inventory maps a friendly Fleet node name to an immutable Keryx peer ID. The schema-v2 operator foundation instead keys explicit policy by managed identity and resolves the current authenticated peer from authoritative binding provenance. Both paths preserve exact deterministic selection. Operator configuration may express policy and presentation metadata, but it does not prove current reachability.

Reachability and routing are determined from Keryx state and the actual submission receipt. Fleet does not silently retarget a current exact-node request to a different node when the selected node is unavailable.

Read-only candidate queries that return more than one eligible node are deliberately separate from exact operator selection. A candidate query reports facts; it does not make an implicit winner-selection decision.

Future automatic scheduling is a separate operation model and must define its own deterministic, explainable selection and retry/admission semantics rather than changing exact-node behavior implicitly.

## Authorization

Fleet is default-deny.

Authorization combines:

- authenticated Keryx sender identity;
- exact destination identity;
- requested operation;
- operator-managed policy;
- managed state projected by Nodescale where present;
- request limits and absolute deadline.

Locally configured deny policy remains authoritative over generated managed grants.

Managed projection can generate only the bounded baseline operations defined by the local contract. It cannot generate `fleet.hermes.run` authority.

Future Recipe execution, environment materialization, profile-sensitive work, or other privileged execution capabilities must remain separately authorizable and must not become implicit Nodescale baseline authority merely because a node is trusted or managed.

## Managed projection

Nodescale can project managed Fleet state through the local `fleet.managed-projection.v1` control interface. The interface is local-only, authenticated through Linux peer credentials, and persisted in Fleet-owned state.

Managed state is separate from:

- operator-owned Fleet inventory and deny policy;
- Keryx task/result storage;
- current operational observations;
- task-to-Hermes-run execution bindings.

The projection contract provides generation-based application, replay detection, conflict detection, and authoritative read-back. See [Managed projection V1](managed-projection-v1.md).

## Nodescale operator control

Nodescale exposes durable device, membership, trust-lifecycle, provider-binding, and Keryx-binding evidence through the separate local read-only `nodescale.operator.v1` Unix-domain API. Fleet's strict client supports only `capabilities`, bounded `devices.list`, and exact `devices.inspect`; it never reads Nodescale SQLite.

Nodescale authenticates the exact configured Fleet service UID through `SO_PEERCRED` before parsing a request. The read path does not reconcile provider trust, prove live Keryx health, admit a device into Fleet, derive readiness, or authorize an operation. Unsupported live facts remain explicitly unavailable. Trust/revoke and invitation mutations remain outside this first slice and require separately typed, revision-fenced contracts with authoritative read-back.

See [Nodescale operator control V1](nodescale-operator-control.md).

## Operational observations and readiness

Managed membership answers whether Fleet knows and admits a node. It does not prove that the node is alive or able to receive useful work.

The Fleet worker publishes one bounded current observation through the local Rust control service. Fleet persists the observation in `fleet-state` and derives liveness and scheduler readiness from managed state, admission generation, receipt freshness, network/Keryx/Hermes/worker availability, and remaining Fleet-owned execution capacity. This capacity describes Fleet's configured local execution slots, not global Hermes admission or non-Fleet work.

The observation also carries the bounded inventory of installed Hermes profile distributions discovered on that node. When an installed profile has the supported Agency V1 behavior shape, Fleet can attach its exact `hermes-agency-profile-content.v1` content digest. Generic or safely unreadable distributions can remain visible by name/version without a fabricated digest.

Observation traffic is local and replaces the current sample; it is not recorded as high-frequency Keryx task rows or an unbounded metrics history. Existing `fleet.health` and `fleet.inventory` responses add the derived readiness/profile view when observation publishing is configured.

See [Node observations and scheduler readiness](node-readiness.md) for fields, freshness, reason codes, profile-presence semantics, and operator configuration.

## Profile identity and Agency source

Hermes Agency defines versioned professional profile distributions. Hermes owns current native profile installation. Fleet owns live distributed evidence of where those profiles are installed and whether those nodes are currently eligible for the current native execution path.

Fleet's current profile-awareness foundation has several distinct layers.

### Installed profile presence

The Fleet node scans the local Hermes profiles directory using bounded distribution rules and publishes canonical profile identities in its current observation.

Presence can therefore be tied to the same admission/freshness fence as other node evidence rather than living in a second registry.

### General and exact ready-carrier lookup

`fleet-state` can find currently admitted, fresh, scheduler-ready nodes that advertise a requested profile, optionally at an exact version.

For Agency packages with a known content digest, exact lookup requires the requested name/content digest and can additionally require the exact version. Digestless or mismatching packages never satisfy exact lookup.

### Pinned Agency snapshots

Fleet can acquire an approved Agency repository at an exact full git object ID, validate the bounded supported runtime catalog, resolve one exact profile package, verify safe distribution identity/path data, and independently recompute the selected Agency V1 content digest.

The result is an immutable package identity bound to a specific snapshot. It is source validation, not installation or execution authority.

The current merged snapshot implementation invokes the pinned Agency catalog generator under bounded execution while validating the approved exact checkout. It must not be generalized into an arbitrary repository execution surface.

### Candidate discovery

`fleet-state` can return currently scheduler-ready admitted nodes that may be considered by a later placement policy. The result includes the current admission generation, available Fleet worker slots, resource observations, same-name installed profile presence when any, and readiness explanation.

The query is deterministic but intentionally does not rank or choose a winner.

### Persistent host installation is not the default future completion path

An earlier design proposed completing profile placement by adding a privileged remote operation that installed a missing Agency profile into the destination host's native Hermes profile directory, then waited for exact fresh post-install observation proof.

That mutation path is not current and is superseded as the default planned architecture by runtime-neutral Fleet Recipes and execution-environment materialization.

Existing native profile observation, exact package identity, and candidate discovery remain useful current contracts. They can inform native execution compatibility and future cache/locality decisions without becoming a requirement that every destination host be preconfigured with a profile.

See [Profile identity, presence, and execution locality](profile-placement.md).

## Planned vNext execution architecture

The following is **planned architecture, not a current Fleet runtime contract**. The frozen direction is defined by [vNext foundation](vnext-foundation.md).

Runtime-neutral Recipe/ResolvedRecipe/ExecutionPlan contracts remain useful inputs to placement and materialization, but the execution lifecycle is now explicitly centered on a persistent Hermes-backed Agent Instance with temporary Fleet authority and a disposable body:

```text
Principal
    ↓
Persistent Hermes Agent Instance
    ├── immutable Agency base
    ├── durable scoped memory
    ├── approved scoped skills
    └── durable Agent metadata
    ↓
Immutable RunAuthority
    ↓
Temporary Run Capsule
    ↓
Fleet-owned disposable execution body
    ↓
Hermes native /v1/runs execution
    ↓
Hermes finalization / quiescence
    ├── persist authorized learning
    ├── revoke run grants
    └── produce evidence
    ↓
destroy disposable body
    ↓
finalize Run Capsule

Persistent Agent Instance remains.
```

Important planned invariants:

- no temporary Hermes profiles and no deleting Agent Instances after jobs;
- no per-run authority, credentials, container IDs, network grants, approval budgets, or host permissions in durable profile configuration;
- Fleet owns disposable runtime lifecycle through mature backends rather than implementing a container runtime or OCI format;
- Docker/OCI is the initial strong backend on suitable Linux hosts, not the definition of a Fleet node;
- backends must advertise their actual isolation/resource guarantees and weaker mechanisms must never claim stronger guarantees;
- Agency package identity is a durable capability-base input, not temporary authority;
- the destination performs authoritative local admission;
- authority can only remain equal or narrow;
- memories, skills, Templar, and model output cannot widen authority;
- security uncertainty fails closed;
- exact-request hashes bind security judgments;
- same-machine execution uses local Fleet + Hermes primitives and does not traverse Keryx;
- Nodescale/Keryx enter only when crossing a machine boundary, establishing remote identity/trust, transporting remote work/results, reconciling remote state, or coordinating distributed execution;
- after a remote hop reaches the destination, destination-local execution stays local;
- scheduler decisions remain deterministic and explainable;
- dirty task state must never be reused as an optimization.

These statements guide future implementation but must not be exposed as shipped capability until the corresponding contracts and proofs are merged.

## Deterministic security-event facts

Fleet's Phase 19 security-event boundary turns already-authoritative request state into immutable, versioned facts for later low-authority evaluation. The canonical `fleet.security-request.v1` projection binds the exact principal, Recipe/ResolvedRecipe identity, proposed immutable RunAuthority hash, target, requested tools, authorized toolsets, resource limits, network posture, policy version, and capability set. Its request hash changes whenever those execution semantics change.

`fleet.security-event.v1` adds bounded memory/skill risk, secret-interception, deterministic policy-mismatch, and quarantine/verification evidence. Those derived facts have a separate event content hash so evidence may change without pretending the underlying request changed. Secret-interception records contain classifications, counts, action, and sanitized evidence identity only; they never contain secret bodies or direct hashes of intercepted secret values.

Deterministic Fleet hard denies are represented separately as `fleet.security-hard-deny.v1` and bind the exact request hash, event hash, and policy digest. They are not embedded as event facts and they are not Templar verdicts. This separation lets later gate logic stop an already-hard-denied request without invoking Templar while keeping the event itself a neutral fact record.

Phase 19 does not grant authority, activate RunAuthority, invoke Templar, or return `ALLOW`, `DENY`, or `REVIEW`. Later Templar phases may consume these exact facts, but Fleet deterministic policy remains authoritative and security judgments must remain bound to the exact request hash.

## Deliberate Hermes execution

`fleet.hermes.run` is the current operation that starts Hermes execution.

The worker uses the authenticated loopback Hermes Runs API and follows this sequence:

1. Reserve the Keryx task in Fleet execution-binding state.
2. Start a Hermes run once.
3. Persist the returned run ID before polling.
4. Poll the exact run while the Keryx task claim remains active.
5. Stop and fail closed if the run requires an approval Fleet cannot safely provide.
6. Persist bounded terminal state before completing the Keryx result.
7. On task reclaim, resume a known run or replay a known terminal result rather than starting a duplicate run.

If Fleet cannot prove whether a run was created, it records an indeterminate condition and fails closed rather than guessing.

Profile presence does not bypass this authorization/execution path. Even a node carrying the exact requested Agency package still needs explicit authority for the executable Fleet operation.

## Durable Fleet state

Fleet intentionally keeps distinct state domains.

### Operator state

Human-managed node inventory and local policy. This remains separate from generated managed state. The canonical active document is the profile-scoped `HERMES_HOME/fleet/nodes.yaml`; schema version 2 can key explicit policy by authoritative managed identity so current Keryx peer bindings are resolved from managed provenance instead of copied into long-lived human-facing configuration. See [Operator foundation](operator-foundation.md).

### Managed projection state

Fleet-owned durable records generated from Nodescale projections, including generations, content identity, provenance, and generated operation sets.

### Observation state

One current typed operational observation per managed node. It preserves last-known facts across restart, rejects out-of-order replacement, and provides the inputs for time-dependent liveness, readiness, resource inspection, and installed profile presence. It is not a telemetry history or a replacement for Keryx state.

Profile lookup and candidate queries are derived from this same durable current state. Fleet does not maintain a second Agency-side or scheduler-side presence registry.

### Execution binding state

A narrow correlation between a Keryx task and a Hermes run. It exists to prevent duplicate execution and permit restart-safe observation. It is not a replacement for Keryx task/result storage.

The Rust `fleet-state` crate provides the durable state foundation for the permanent Fleet implementation. The Python implementation remains the current integration reference while Rust surfaces continue to expand.

## Trusting response data

Authentication and content trust are separate.

Peer-originated health, inventory, message acknowledgements, profile claims, and Hermes output are presented as untrusted data. Remote fields cannot override controller-owned target selection, the Keryx task ID, authenticated peer identity, delivery route, local authorization, managed-state provenance, or current admission generation.

The local observation boundary performs its own validation before profile/resource facts become Fleet state. A remote task response must not be allowed to masquerade as authoritative installed-profile presence.

This distinction matters even on private networks: authenticated machines can still return malformed, stale, compromised, or model-generated content.

## Deadlines and limits

The absolute Keryx deadline is the source of truth for cross-node work.

Fleet rejects already-expired requests before handler execution and passes only the remaining budget into downstream operations. Health probes share one remaining budget rather than receiving independent full timeouts. Executable work stops observation at the deadline and attempts bounded cleanup where supported.

Payload, prompt, response, profile-inventory, and source/catalog sizes are bounded by their respective Fleet and Keryx contracts.

## Cancellation

Fleet exposes a cancellation surface, but cross-node running-task cancellation currently fails closed. Recording cancellation at the origin is insufficient proof that the remote worker observed the request and stopped an already-bound Hermes run.

Fleet therefore does not claim successful remote cancellation until Keryx and the worker can provide that evidence end to end.

## Implementation strategy

Hermes Fleet remains one product while the implementation evolves.

- The **Python implementation** is the proven Hermes plugin/runtime and compatibility reference and currently owns several integration surfaces, including local installed-profile scanning and Agency snapshot acquisition.
- The **Rust implementation** provides permanent domain, durable state, managed-control, observation/readiness, profile-presence, and candidate-query foundations and will progressively assume additional runtime responsibilities.
- Language-neutral fixtures capture behavior that must remain compatible between implementations.

Compatibility is defined by externally meaningful contracts and tests, not by importing Python internals into Rust or vice versa.

## Deployment boundary

A typical current deployment keeps:

- Keryx daemons and Hermes Runs APIs on loopback;
- relay/control services on explicitly secured private interfaces;
- Fleet managed projection/observation on a private local Unix socket;
- node tokens, private keys, TLS keys, and Hermes credentials outside Git;
- Fleet worker state private to its service account;
- `fleet-node` as the local owner of Fleet operation dispatch and skill registration.

Agency profile packages used by the current native execution path are installed into Hermes through Hermes-supported profile tooling. Fleet observes the resulting local installation rather than treating an upstream catalog as live runtime state.

Future Recipe materialization will require its own explicitly documented backend/deployment contract before it becomes current behavior.

See [Deployment](deployment.md) for the current generic service layout.

## Non-goals and non-claims

Fleet does not currently attempt to provide:

- a second message transport or durable mailbox;
- a general executable workflow engine;
- implicit role-to-execution authorization;
- broadcast or pub/sub semantics;
- multi-node consensus;
- automatic trust promotion from hostnames, addresses, tags, or mesh membership;
- a replacement for Hermes local execution state;
- a second live profile registry separate from Fleet observations;
- persistent automatic remote host-profile installation as the default placement mechanism;
- Fleet Recipe execution as a current runtime contract;
- disposable task environments as a current runtime contract;
- a homegrown container runtime, OCI format, VPN, artifact transport, package manager, or registry.
