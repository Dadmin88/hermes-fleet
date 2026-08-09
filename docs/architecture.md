# Hermes Fleet Architecture

Hermes Fleet is an application-level coordination layer built on Hermes Keryx. Keryx supplies authenticated peer transport and durable task/result delivery; Fleet adds stable operator identity, exact-node selection, authorization, operation dispatch, and the narrow state required to coordinate deliberate Hermes execution.

Fleet does not implement a parallel relay, task ledger, workflow engine, or mailbox.

## Authority model

Four systems participate in the full trust chain:

1. **Nodescale** owns managed membership and desired managed Fleet state.
2. **Keryx** owns authenticated transport peer identity, routing, task/result durability, claims, leases, and relay behavior.
3. **Fleet** owns application-level node identity, local authorization, operation dispatch, managed-state reconciliation, and execution correlation.
4. **Hermes** owns local agent execution and its models, tools, skills, credentials, permissions, memory, and sessions.

These authorities are deliberately not interchangeable.

- A device being present on the private network does not grant Fleet authority.
- A Keryx-authenticated peer is not automatically authorized for every Fleet operation.
- A Fleet role, tag, or managed membership record does not automatically authorize Hermes execution.
- A peer-produced response is still untrusted content even when its sender identity is authenticated.

## Operation model

Fleet exposes a small versioned operation vocabulary:

| Operation | Class | Creates a Hermes run | Description |
| --- | --- | ---: | --- |
| `fleet.health` | direct | No | Bounded Fleet/Keryx/Hermes capability health. |
| `fleet.inventory` | direct | No | Safe node identity and capability summary. |
| `fleet.message` | direct | No | Bounded text communication and acknowledgement. |
| `fleet.hermes.run` | executable | Yes | Deliberately start and observe one local Hermes run. |

Every request uses the same bounded Fleet envelope over Keryx. The worker validates the authenticated sender, destination, operation, envelope version, Keryx metadata, absolute deadline, payload limits, and local policy before choosing a handler.

Direct operations never enter the Hermes execution path.

## Request flow

A typical cross-node request follows this path:

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

Operator inventory maps a friendly Fleet node name to an immutable Keryx peer ID. Selection is exact and deterministic. Inventory configuration may express operator policy and presentation metadata, but it does not prove current reachability.

Reachability and routing are determined from Keryx state and the actual submission receipt. Fleet does not silently retarget a request to a different node when the selected node is unavailable.

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

## Managed projection

Nodescale can project managed Fleet state through the local `fleet.managed-projection.v1` control interface. The interface is local-only, authenticated through Linux peer credentials, and persisted in Fleet-owned state.

Managed state is separate from:

- operator-owned Fleet inventory and deny policy;
- Keryx task/result storage;
- task-to-Hermes-run execution bindings.

The projection contract provides generation-based application, replay detection, conflict detection, and authoritative read-back. See [Managed projection V1](managed-projection-v1.md).

## Operational observations and readiness

Managed membership answers whether Fleet knows and admits a node. It does not prove that the node is alive or able to receive useful work.

The existing Fleet worker publishes one bounded current observation through the local Rust control service. Fleet persists the observation in `fleet-state` and derives liveness and scheduler readiness from managed state, receipt freshness, network/Keryx/Hermes/worker availability, and remaining Fleet-owned execution capacity. This capacity describes Fleet's configured local execution slot, not global Hermes admission or non-Fleet work. Readiness is explainable through machine-readable reasons and is recomputed rather than stored as a second authority.

Observation traffic is local and replaces the current sample; it is not recorded as high-frequency Keryx task rows or an unbounded metrics history. Existing `fleet.health` and `fleet.inventory` responses add the derived readiness view when observation publishing is configured.

See [Node observations and scheduler readiness](node-readiness.md) for fields, freshness, reason codes, and operator configuration.

## Deliberate Hermes execution

`fleet.hermes.run` is the only initial operation that starts Hermes execution.

The worker uses the authenticated loopback Hermes Runs API and follows this sequence:

1. Reserve the Keryx task in Fleet execution-binding state.
2. Start a Hermes run once.
3. Persist the returned run ID before polling.
4. Poll the exact run while the Keryx task claim remains active.
5. Stop and fail closed if the run requires an approval Fleet cannot safely provide.
6. Persist bounded terminal state before completing the Keryx result.
7. On task reclaim, resume a known run or replay a known terminal result rather than starting a duplicate run.

If Fleet cannot prove whether a run was created, it records an indeterminate condition and fails closed rather than guessing.

## Durable Fleet state

Fleet intentionally keeps distinct state domains.

### Operator state

Human-managed node inventory and local policy. This remains separate from generated managed state.

### Managed projection state

Fleet-owned durable records generated from Nodescale projections, including generations, content identity, provenance, and generated operation sets.

### Observation state

One current typed operational observation per managed node. It preserves last-known facts across restart, rejects out-of-order replacement, and provides the inputs for time-dependent liveness and readiness derivation. It is not a telemetry history or a replacement for Keryx state.

### Execution binding state

A narrow correlation between a Keryx task and a Hermes run. It exists to prevent duplicate execution and permit restart-safe observation. It is not a replacement for Keryx task/result storage.

The Rust `fleet-state` crate provides the durable state foundation for the permanent Fleet implementation. The Python implementation remains the current integration reference while Rust surfaces continue to expand.

## Trusting response data

Authentication and content trust are separate.

Peer-originated health, inventory, message acknowledgements, and Hermes output are presented as untrusted data. Remote fields cannot override controller-owned target selection, the Keryx task ID, authenticated peer identity, delivery route, local authorization, or managed-state provenance.

This distinction matters even on private networks: authenticated machines can still return malformed, stale, compromised, or model-generated content.

## Deadlines and limits

The absolute Keryx deadline is the source of truth for cross-node work.

Fleet rejects already-expired requests before handler execution and passes only the remaining budget into downstream operations. Health probes share one remaining budget rather than receiving independent full timeouts. Executable work stops observation at the deadline and attempts bounded cleanup where supported.

Payload, prompt, and response sizes are bounded by Fleet policy and the underlying Keryx transport contracts.

## Cancellation

Fleet exposes a cancellation surface, but cross-node running-task cancellation currently fails closed. Recording cancellation at the origin is insufficient proof that the remote worker observed the request and stopped an already-bound Hermes run.

Fleet therefore does not claim successful remote cancellation until Keryx and the worker can provide that evidence end to end.

## Implementation strategy

Hermes Fleet remains one product while the implementation evolves.

- The **Python implementation** is the proven Hermes plugin/runtime and compatibility reference.
- The **Rust implementation** provides the permanent domain and durable state foundations and will progressively assume additional runtime responsibilities.
- Language-neutral fixtures capture behavior that must remain compatible between implementations.

Compatibility is defined by externally meaningful contracts and tests, not by importing Python internals into Rust or vice versa.

## Deployment boundary

A typical deployment keeps:

- Keryx daemons and Hermes Runs APIs on loopback;
- relay/control services on explicitly secured private interfaces;
- node tokens, private keys, TLS keys, and Hermes credentials outside Git;
- Fleet worker state private to its service account;
- `fleet-node` as the local owner of Fleet operation dispatch and skill registration.

See [Deployment](deployment.md) for a generic service layout.

## Non-goals

Fleet does not currently attempt to provide:

- a second message transport or durable mailbox;
- a general workflow engine;
- implicit role-to-execution authorization;
- broadcast or pub/sub semantics;
- multi-node consensus;
- automatic trust promotion from hostnames, addresses, tags, or mesh membership;
- a replacement for Hermes local execution state.
