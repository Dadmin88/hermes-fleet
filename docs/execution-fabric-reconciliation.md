# Execution Fabric current-state reconciliation

This document records the shipped execution-fabric foundation and the historical FX1 boundary after exact-target Desktop execution. It is not a claim that Recipe execution, automatic placement, or environment backends are available. For planned work beyond that foundation, the frozen authority/lifecycle direction is now [vNext foundation](vnext-foundation.md); any older planned sequence in this document is subordinate to that contract.

## Current shipped layers

### Workflow authoring

`fleet.workflow-editor.v1` is a durable authoring document stored as immutable numbered revisions by `fleet-state`. Every authored node remains runtime-unavailable. Workflow definitions are not Recipes, execution plans, Keryx tasks, or a scheduler queue.

### Exact native execution

`fleet.hermes.run` resolves one stable managed identity to its current authenticated Keryx binding, applies explicit local policy and readiness, creates one durable Keryx task, and invokes the worker's authenticated loopback Hermes Runs API. The worker resumes or reports indeterminate state instead of starting a duplicate run when creation cannot be proven.

This path assumes the required Hermes profile/configuration is already installed on the destination. It performs no per-task package or environment materialization.

### Agency and profile identity

Fleet can acquire an approved Agency repository at one exact git revision and independently verify a profile package's name, version, and `hermes-agency-profile-content.v1` digest. Fleet also observes current native profile presence on workers.

That machinery proves immutable profile identity and native locality. It does not install a profile, authorize execution, choose a node, or define a runtime backend.

### Durable state ownership

- Keryx owns durable task, result, artifact, and routing truth.
- Fleet operator configuration owns explicit local operation policy.
- Fleet managed projection owns admitted stable identity and binding generations.
- Fleet observation state owns one bounded current readiness/capacity/profile sample per managed node.
- Fleet execution binding state owns only task-to-Hermes-run correlation and duplicate-safe recovery.
- Fleet workflow tables own authoring revisions only.

No Recipe state currently exists, and FX1 does not require a new database.

## Reconciled implementation sequence

The FX1 Recipe/ResolvedRecipe/ExecutionPlan layers remain valid planning inputs, but vNext wraps them in the persistent-brain / temporary-authority lifecycle:

```text
FleetRecipe
    logical runtime-neutral requirements
        ↓ explicit resolution
ResolvedRecipe
    exact immutable ingredient identities
        ↓ backend planning / placement / local admission
ExecutionPlan
        ↓
Persistent Hermes Agent Instance
        ↓
Immutable RunAuthority
        ↓
Temporary Run Capsule
        ↓
Fleet-owned disposable execution body
        ↓
Hermes native /v1/runs
        ↓
finalization / quiescence
        ↓
destroy body; preserve Agent Instance
```

Keryx is inserted only when the request/result actually crosses a machine boundary. It is not part of the same-machine lifecycle by default.

## FX1 boundary

The smallest coherent FX1 slice is a language-neutral contract module with strict bounded parsing and canonical serialization for:

- `FleetRecipe`: requested Hermes agent/profile requirement plus runtime-neutral environment, resource, security, and namespaced extension requirements;
- `ResolvedRecipe`: the exact Recipe identity plus exact immutable Agency source revision and verified package/content identity selected to satisfy it;
- deterministic content hashes suitable for later request correlation;
- preservation of bounded namespaced extension data for forward compatibility.

FX1 must not:

- reference Docker, OCI, PRoot, or any backend;
- select or rank a Fleet node;
- encode Keryx peers or mutable host paths;
- persistently install a profile;
- execute a workflow or duplicate its authoring schema;
- grant `fleet.hermes.run`;
- add scheduler, admission, materialization, cleanup, or retry behavior;
- alter current exact native execution semantics.

A later execution request can carry a `ResolvedRecipe` into backend planning without overloading the current prompt-only `fleet.hermes.run` envelope.

## Primary risks

1. **Workflow/Recipe conflation** — authoring graph structure is not a logical environment requirement.
2. **Recipe/resolution collapse** — mutable names or ranges cannot appear as if they were immutable selected ingredients.
3. **Backend leakage** — Docker fields in `FleetRecipe` would make one implementation the domain model.
4. **Locality as authority** — observed package/cache presence cannot grant execution or bypass admission.
5. **Persistent host mutation** — package resolution must not revive superseded remote profile installation.
6. **Duplicate durable owners** — Recipes must not create another task ledger, artifact store, or runtime state machine.
7. **Opaque extension escalation** — preserving unknown namespaced data does not mean Fleet understands or authorizes it.
