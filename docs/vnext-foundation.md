# Hermes Fleet vNext foundation

This document freezes the architecture, terminology, ownership boundaries, and hard invariants for the Hermes Fleet vNext implementation program.

It is intentionally a **planned-architecture contract**, not a claim that every described capability is already shipped. Current merged implementation and executable tests remain the source of truth for what exists today. When older planned documentation conflicts with this document, this document is authoritative for vNext direction.

## Core ownership boundaries

### Hermes Agent

Hermes Agent owns the durable local agent runtime and its native primitives:

- persistent Agent Instances;
- native profiles;
- model/provider routing;
- `/v1/runs`;
- tools and toolsets;
- terminal execution;
- approvals;
- sessions and SessionDB;
- memory primitives;
- skills primitives;
- interruption;
- process evidence;
- finalization and quiescence;
- local execution primitives.

Fleet must use these native primitives for local agent behavior rather than reimplementing a parallel agent runtime.

### Hermes Agency

Hermes Agency owns immutable professional capability bases:

- profile definitions;
- skills bundled into Agency bases;
- exact pinned source material.

Agency bases are durable capability definitions. They are not per-run authority and are not disposable execution state.

### Hermes Fleet

Fleet owns policy, execution lifecycle, and temporary authority:

- authorization;
- admission;
- placement;
- scheduling;
- reservations;
- immutable `RunAuthority`;
- temporary `RunCapsule` state;
- disposable runtime ownership;
- resource limits;
- network and filesystem grants;
- structured host-action authority;
- learning promotion policy;
- Templar gate orchestration;
- audit and provenance.

Fleet may orchestrate mature runtime primitives such as OCI/Docker. It does not become a container runtime implementation, transport layer, or agent runtime.

### Hermes Nodescale

Nodescale owns cross-machine identity and trust:

- machine/device identity;
- membership;
- trust;
- principal/device trust relationships;
- cross-machine identity projection.

Nodescale must not be pulled into work that remains entirely on one machine merely to manufacture local identity or transport.

### Hermes Keryx

Keryx owns authenticated **inter-machine** task transport:

- authenticated peer identity for transport;
- routing;
- delivery;
- bounded redelivery;
- result transport.

Keryx is not the intra-machine execution path. Fleet must not bounce same-machine work through Keryx.

### Templar

Templar is a low-authority evaluator only.

It may return exactly:

- `ALLOW`;
- `DENY`;
- `REVIEW`.

Templar does not:

- grant authority;
- operate nodes;
- invoke arbitrary tools;
- control Fleet;
- control Keryx;
- control Docker;
- widen an existing authority decision.

A Templar `ALLOW` verdict never grants execution by itself. Deterministic Fleet policy remains authoritative.

### Vault

Vault owns secret material and secret lifecycle:

- secret bodies;
- versioning;
- rotation;
- scoped references;
- temporary run handles.

Persistent Agent state and durable Fleet metadata may contain authorized references, never secret bodies merely for convenience.

## Hard invariants

The following rules are non-negotiable across the vNext implementation:

1. No temporary Hermes profiles.
2. No deleting persistent Agent Instances after jobs.
3. No per-run config rewrites into durable profile state.
4. No per-run container IDs in persistent profile config.
5. No approval budgets in persistent profile config.
6. No temporary credentials in persistent profile config.
7. No network grants in persistent profile config.
8. No `RunAuthority` in persistent profile config.
9. No host permissions in memory or skills.
10. Memories cannot widen authority.
11. Skills cannot widen authority.
12. Templar cannot widen authority.
13. Model output cannot widen authority.
14. Authority can only remain equal or narrow as execution proceeds.
15. Security uncertainty fails closed.
16. Exact-request hashes bind all security judgments.

These invariants apply even when a shortcut would be operationally convenient.

## Machine-boundary invariant

For work occurring entirely on one machine:

- use Hermes native local primitives;
- use local Fleet logic where Fleet owns policy or lifecycle;
- do not bounce work through Keryx;
- do not involve Nodescale transport machinery unnecessarily.

Nodescale and Keryx enter the execution path only when the operation actually crosses a machine boundary or needs distributed-state reconciliation.

They are appropriate for:

- crossing a machine boundary;
- establishing remote machine identity and trust;
- transporting a remote job;
- reconciling remote state;
- coordinating distributed execution.

The boundary is architectural, not merely an optimization.

## Workflow → Recipe boundary

Fleet's durable Workflow Library/Canvas is the human-facing orchestration layer.
A backend-owned immutable Workflow revision may be compiled into one or more
backend-neutral Recipes, but the editor is never an authority source.

```text
Workflow revision
   |
   v
Deterministic Workflow compiler
   |
   +-- Candidate Recipe(s): declared/derived/discovered/proposed/unknown facts
   |
   v
Validated Recipe(s)
   |
   v
Resolved Recipe(s)
   |
   v
later admission / RunAuthority / Run Capsule
```

Recipes are the Fleet-native Compose-style description of what each disposable
execution body requires: CPU, RAM, GPU, storage, runtime/toolchains, filesystem,
network, toolsets, symbolic secret needs, structured host-operation needs and
execution/placement constraints. These are requirements, not grants. Unknown
mandatory requirements and unvalidated proposals fail closed before authority.

A Workflow edge may establish orchestration/dependency facts only. It cannot
widen network, filesystem, secret, broker, host, container, model or Agent
authority. Discovery runs in a lower-authority disposable body and produces
evidence; it does not authorize its own findings.

## Canonical lifecycle

```text
Principal
   |
   v
Persistent Hermes Agent Instance
   |
   +-- immutable Agency base
   +-- durable scoped memory
   +-- approved scoped skills
   +-- durable Agent metadata
   |
   v
Immutable RunAuthority
   |
   v
Temporary Run Capsule
   |
   +-- principal
   +-- Recipe
   +-- approvals
   +-- secret references
   +-- network/filesystem grants
   +-- host-broker grants
   +-- resource/deadline limits
   +-- exact container binding
   |
   v
Fleet-owned disposable OCI container
   |
   v
Hermes native /v1/runs execution
   |
   v
Hermes finalization / quiescence
   |
   +-- persist authorized learning
   +-- revoke run grants
   +-- produce evidence
   |
   v
Destroy container
   |
   v
Finalize Run Capsule

Persistent Agent Instance remains.
```

## Durable brain, disposable body

The durable and temporary state split is fundamental:

**Durable**

- Agent Instance identity;
- immutable Agency base identity;
- authorized scoped memories;
- approved scoped learned skills;
- durable Agent metadata;
- required provenance/history.

**Temporary per run**

- `RunAuthority`;
- `RunCapsule`;
- approval budget;
- temporary secret handles;
- network/filesystem grants;
- host-broker grants;
- exact container identity;
- resource and deadline limits;
- run-scoped Hermes execution overrides.

Destroying a run body must not destroy the Agent brain. Preserving an Agent brain must not preserve run authority.

## Local and remote execution shapes

### Local execution

```text
local authenticated principal
    -> Fleet deterministic policy/admission
    -> immutable RunAuthority
    -> temporary RunCapsule
    -> local disposable runtime
    -> local Hermes /v1/runs
    -> finalization/quiescence
    -> evidence + authorized learning
    -> destroy runtime
```

No Keryx hop is inserted merely because Fleet is involved.

### Remote execution

```text
origin principal / controller
    -> Fleet authorization of remote intent
    -> Nodescale-backed remote identity/trust facts
    -> Keryx authenticated inter-machine transport
    -> destination Fleet admission
    -> destination-local immutable RunAuthority
    -> destination-local RunCapsule
    -> destination-local disposable runtime
    -> destination-local Hermes /v1/runs
    -> finalization/quiescence
    -> destination cleanup
    -> Keryx result/evidence transport
```

After the inter-machine hop, local work on the destination remains local.

## Authority monotonicity

Every temporary power used during execution must derive from the exact authorized request and may only remain equal or narrow.

The following are never sources of new authority:

- prompts;
- model output;
- memories;
- learned skills;
- Agency profile content;
- Templar verdict text;
- peer-produced result content;
- stale cached policy or capability state.

Any later phase that cannot prove this property fails closed.

## Terminology

Use these names consistently in code, docs, tests, UI, and operator surfaces:

- **Agent Instance**: persistent Hermes-backed agent identity and durable brain.
- **Agency base**: immutable professional capability base used by an Agent Instance.
- **RunAuthority**: immutable, exact, temporary authorization object for one execution intent.
- **Run Capsule**: Fleet-owned temporary execution/lifecycle state derived from one RunAuthority.
- **disposable runtime/body**: temporary OCI or other explicitly supported execution environment bound to one Run Capsule.
- **Templar**: low-authority evaluator that returns `ALLOW`, `DENY`, or `REVIEW` and never grants power.
- **Vault reference**: scoped opaque reference to secret material owned by Vault.
- **host-action broker**: typed Fleet-controlled bridge for narrowly authorized host effects, never arbitrary host shell authority.

Do not reintroduce the retired disposable-profile model under another name.

## Phase-order rule

The vNext implementation program proceeds from Phase 0 forward. Work already present in branches, worktrees, experiments, or merged code may be reused only after it is audited against the requirements and invariants of the phase currently being closed.

Being implemented earlier does not make a later-phase feature accepted. Each phase is closed by evidence, not chronology.
