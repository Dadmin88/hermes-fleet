# ExecutionBackend contract

`ExecutionBackend` is the provider-neutral lifecycle boundary that realizes an exact `ResolvedRecipe` through one mature runtime.

```text
FleetRecipe
  → ResolvedRecipe
  → ExecutionPlan
  → ExecutionBackend
  → backend-owned realization
```

## ExecutionPlan

An `ExecutionPlan` binds:

- an exact execution identity;
- an idempotency key;
- one immutable `ResolvedRecipe`;
- the exact capability-document hash accepted for realization.

It does not select a node or grant authority.

## Lifecycle

Every backend implements:

- `capabilities` — current hard guarantees;
- `prepare` — centrally validate the current capability hash, then idempotently materialize one exact plan fingerprint without starting;
- `start` — idempotently start without duplicate work;
- `inspect` — read authoritative lifecycle state;
- `stop` — stop, or report an indeterminate outcome when exact stop cannot be proven;
- `cleanup` — idempotently remove backend-owned resources and return `cleaned` only after authoritative absence is observed.

The durable handle separates Fleet execution identity from the backend's opaque realization identity and retains the exact plan fingerprint. Reusing an execution identity with a different idempotency key, Recipe, or capability document is a conflict. Lifecycle transitions reject regressions and keep `indeterminate` distinct from failure, completion, and stopped. An indeterminate handle cannot transition directly to `cleaned`; the backend must reconcile provider state and prove the realization is absent.

## Ownership boundaries

This contract does not:

- implement a container runtime;
- encode Docker commands or OCI as the general interface;
- place or schedule work;
- submit Keryx tasks;
- authorize execution;
- install Agency profiles persistently;
- own result or artifact transport.

A later exact-node Recipe execution slice can bind this lifecycle to existing Fleet authorization, readiness, Keryx transport, and Hermes Agent execution.
