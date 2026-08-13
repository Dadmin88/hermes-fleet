# Private-network vertical-slice acceptance V1

## Status

**Accepted on 2026-08-13.**

This record freezes the first complete private-network Hermes Fleet execution across two real machines. It is an acceptance checkpoint for the existing architecture, not a proposal to reopen or redesign it.

The machines are identified here only by their roles:

- `controller`
- `remote-worker`

Exact task, run, device, peer, host, address, path, and service-instance evidence is retained in local operator state and is intentionally excluded from this public document.

## What was proven

One exact-target `fleet.hermes.run` request crossed the complete production path:

```text
controller
  -> explicit Fleet execution authorization
  -> exact Fleet target selection
  -> durable authenticated Keryx task
  -> authenticated relay delivery
  -> exact remote peer
  -> remote Fleet worker
  -> authenticated loopback Hermes Runs API
  -> real Hermes execution
  -> durable terminal Fleet/Keryx result
  -> controller
```

The accepted task:

- selected the intended `remote-worker` exactly;
- used the Keryx relay route;
- was authenticated and durably recorded by Keryx;
- required explicit local Fleet policy for `fleet.hermes.run`;
- created exactly one real Hermes run;
- reached terminal `completed` state;
- returned the expected deterministic marker;
- persisted one completed Fleet task-to-run binding;
- left no indeterminate Fleet execution binding.

This acceptance proves the integrated vertical slice, not automatic scheduling, Recipe execution, persistent profile provisioning, executable workflows, or multi-controller coordination.

## Authority boundaries preserved

The proof crossed, but did not collapse, the product boundaries:

| Layer | Accepted responsibility |
| --- | --- |
| Private network | Reachability between the two machines. |
| Hermes Nodescale | Stable device identity, explicit trust, and the authenticated device-to-Keryx binding used for managed admission. |
| Hermes Keryx | Authenticated peer transport, durable task/result state, relay delivery, and terminal result delivery. |
| Hermes Fleet | Managed state, readiness, exact-target selection, explicit operation authorization, capacity accounting, dispatch, and task-to-run correlation. |
| Hermes Agent | The actual remote agent run and terminal response. |

The acceptance therefore preserves these distinctions:

```text
connected
!= trusted
!= Keryx authenticated and bound
!= Fleet managed
!= scheduler ready
!= authorized to execute Hermes
```

Nodescale-managed projection did not grant `fleet.hermes.run`. Execution authority remained an explicit Fleet-local decision.

This is **Milestone One: The Fleet Exists**. It is not Fleet Recipe or Execution Fabric acceptance. Recipe resolution, backend-specific execution plans, disposable environment materialization, destination-side plan admission, and cleanup remain future work.

## Integrity at acceptance

After terminal completion:

- relevant Fleet, Keryx, and Hermes services on both roles were active;
- Fleet-owned and Keryx-owned SQLite stores passed `PRAGMA quick_check`;
- the accepted task had exactly one completed Fleet binding;
- the accepted binding referenced exactly one Hermes run;
- the accepted result matched the expected deterministic marker;
- the Keryx terminal result was delivered to the controller;
- no Fleet binding remained indeterminate.

Exact row identities and private topology values are intentionally retained only in the local evidence bundle.

## Accepted revisions

The public source baseline at acceptance was:

- Hermes Fleet: `e5998c204bdce0d52059e98db47aede23f01a4b4`
- Hermes Keryx Python SDK/main: `7862afb8fd425f75a6371ec04420a82de0abd46f`

The Fleet revision includes the final protocol-feature preservation correction. The Keryx revision includes Python registration refresh preservation for `AgentCard.protocol_features`.

Other machine-specific deployment provenance, including installed binary hashes and service configuration evidence, remains in the private local evidence bundle.

## Acceptance defects and regression expectations

The live path exposed concrete defects and operational hazards. They are now regression expectations rather than unwritten deployment knowledge.

### Shared registration must preserve Keryx baseline protocol features

The Rust Keryx edge and Python Fleet worker shared one authenticated peer identity. Registry registration uses whole-card replacement semantics. A Fleet refresh that omitted Keryx baseline features erased them and caused relay rejection before remote delivery.

Every Fleet Python node registration must preserve these protocol features:

- `absolute_deadlines_v1`
- `result_artifact_bytes_v1`

They are protocol metadata only. They must not become Fleet operations, task skills, worker operations, generated grants, or durable task operation IDs.

### Observation advertisement remains additive

When the worker is configured to receive remote observation publication, it may additionally advertise:

- `fleet.observation.publish.v1`

That optional feature must be added to the baseline pair, never replace it. A worker without the option must continue to advertise the baseline pair.

### Terminal-result unavailability must not discard authoritative task state

Keryx may expose a terminal task status while its terminal result payload is temporarily unavailable. Fleet reconciliation must retain the task handle's authoritative terminal status, consult the exact Hermes run when one is bound, and resolve only when the combined evidence disproves continued execution.

Ordinary transport or inspection failures remain fail-closed.

### Indeterminate work consumes capacity until reconciled

An indeterminate execution binding is not free capacity. Fleet must retain it, count it conservatively against Fleet-owned execution slots, and reconcile it through public durable Keryx and exact Hermes run authority. Operational recovery must not require database editing.

### One Hermes gateway owner per profile

Live acceptance found competing Hermes gateway service ownership for one profile. The accepted deployment restored one canonical owner before execution.

Future operator diagnostics must detect duplicate or conflicting gateway units and report them without deleting services automatically.

### Credential reload must preserve identity and fail closed

Keryx daemon authentication credentials were converged without changing peer identity, durable state, or network topology. Reload/restart handling must preserve those identities and must prove that authenticated requests succeed while unauthenticated mutation requests are rejected.

Credential values must never appear in public fixtures, logs, documentation, or diagnostic output.

## Scope of this checkpoint

This checkpoint establishes that the private-network exact-target execution substrate works on real machines and that its final durable state is internally consistent.

It does not authorize skipping later acceptance gates. Operator APIs, CLI, Desktop execution, scheduling, additional workers, profile provisioning, workflows, recovery hardening, and packaging each require their own bounded implementation and proof.
