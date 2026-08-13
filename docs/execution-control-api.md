# Execution control API

`fleet.execution-control.v1` exposes the already-shipped FX6 durable `ExecutionInstance` and FX7 destination-admission contracts through the authenticated destination-local Fleet Unix socket.

It is a prerequisite for exact-node Recipe execution, not an execution engine.

## Operations

- `reserve_admit` revalidates current managed identity, binding/admission generations, authenticated Keryx binding, caller-supplied explicit operation authorization, readiness, available worker slots, capability snapshot, and deadline. It creates the durable instance only after admission succeeds. Exact replays return the existing instance; conflicting identity fails closed.
- `get` reads one bounded durable instance for recovery.
- `transition` applies a generation-fenced lifecycle transition using the FX6 state machine.

Every request and response uses the existing length-prefixed local control transport and inherits its UID/socket authorization. Unknown members and invalid lifecycle documents are rejected.

`hermes_fleet.execution_control.ExecutionControlClient` is the strict Python adapter used by the destination worker. It validates the complete instance envelope, typed admission outcomes, current capability hash, and generation-fenced transition responses rather than parsing human text or reading Fleet SQLite.

## Authority boundary

The service derives managed state, binding evidence, admission generation, readiness, and capacity from the owning Rust state store at decision time. The caller does not supply those facts. The caller must supply its explicit local `fleet.hermes.run` policy verdict and the backend's freshly inspected capability hash because policy configuration and backend runtime state remain outside Fleet's state database. Admission compares that current hash against the instance's pinned requirement and fails closed on drift.

A denied admission does not create an execution instance. A positive admission is a point-in-time decision, not a capacity lease. FX8 must consume it immediately and record every backend/Keryx transition through this API. Placement and reservations remain deferred.

## Not implemented here

- Recipe resolution or materialization;
- backend preparation/start/cleanup;
- Keryx submission or result storage;
- secret injection;
- scheduling or capacity reservation.
