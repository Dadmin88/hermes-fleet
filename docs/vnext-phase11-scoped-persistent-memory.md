# Hermes Fleet vNext Phase 11 acceptance: scoped persistent memory

Status: **CLOSURE GATED**

Phase 11 is complete only when the implementation commit is merged with green
pull-request CI and the resulting `main` commit also has green CI. Until both
conditions are true, this document is an acceptance candidate rather than a
phase-completion claim.

Phase 11 keeps persistent learning in Hermes Agent's native memory subsystem and
adds Fleet-owned authorization around it. Fleet does not create a second memory
engine, does not persist per-run authority in an Agent profile, and does not give
the model authority to choose its own memory scope.

## Ownership boundary

Hermes Agent owns:

- the native persistent memory files/store;
- scoped memory metadata and storage layout;
- retrieval filtering before system-prompt construction;
- atomic memory mutation mechanics;
- secret/body rejection in Fleet-scoped memory;
- RunAuthority-material rejection in Fleet-scoped memory;
- entry retention/revocation visibility rules;
- promotion-state visibility for shared scopes.

Fleet owns:

- the authenticated principal;
- the exact persistent Agent Instance;
- the immutable RunAuthority and Run Capsule;
- which read scopes are authorized for an exact run;
- the only permitted write scope;
- current-principal validation before submission;
- explicit calls to Hermes's scoped-memory write API.

The disposable OCI body owns none of this state.

## Hermes Agent prerequisite

Fleet pins the verified Hermes Agent commit:

`64094d4c8839b155f23b1969150e9197a36be941`

That Agent revision advertises and implements:

- `run_fleet_memory_scope`;
- `fleet_scoped_memory_write`;
- `fleet-memory-v1` ContextVar run scoping;
- principal-private native memory storage;
- promoted shared-scope retrieval;
- native-memory metadata for owner principal, scope, source run, Agent Instance,
  sensitivity, trust, promotion state, retention, provenance and timestamps.

Fleet CI includes a clean-install compatibility smoke for the exact
`fleet-memory-v1` request shape so a future incompatible Agent change cannot be
silently accepted.

## Fleet memory authorization

`hermes_fleet.scoped_memory.authorize_scoped_memory()` derives memory access only
from already-authorized Fleet state:

```text
current PrincipalReference
+ current PrincipalRecord
+ exact RunCapsuleSpec projected from RunAuthority
                    |
                    v
          fleet-memory-v1 binding
```

Every binding contains the exact:

- principal ID, kind, generation and binding hash;
- Agent Instance ID;
- Fleet execution ID as source run;
- authorized read scopes;
- principal-private write scope;
- optional retention deadline.

Fleet refuses the binding if the PrincipalRecord no longer equals the Capsule's
exact `PrincipalReference` or if the authorized read set exceeds Hermes's
bounded scope count.

## Scope rules

Principal-private memory is always the first read scope and is the **only**
writable scope.

Shared reads are narrowly derived:

- `project`: only project IDs already present in the immutable RunAuthority's
  `project_scope` projection;
- `network`: only the network identity already bound into the current principal;
- `owner`: only the owner identity already bound into the current principal;
- `agent_instance`: only the exact persistent Agent Instance bound to the run.

A durable principal membership does not silently widen an omitted RunAuthority
project scope.

Non-principal scopes are retrieval-only in Phase 11. Hermes independently
requires those entries to have `promotion_state == "promoted"` before they are
visible. Therefore merely authorizing a shared read scope cannot create or
publish shared memory.

## Run submission and stale-authority handling

Immediately before Hermes submission, Fleet re-validates:

1. the immutable RunAuthority;
2. the exact current principal reference;
3. the scoped-memory derivation.

Only then does Fleet send both:

- `fleet_runtime`, bound to the exact disposable container; and
- `fleet_memory`, bound to the exact principal and Agent Instance.

If principal or authority state changed after body creation, Fleet fails the run
and performs the existing no-run cleanup path instead of submitting with stale
memory authority.

Hermes itself fails Fleet-runtime runs closed when no Fleet memory binding is
present, and its scoped memory is carried through ContextVar state rather than
persistent profile configuration.

## Explicit writes

`HermesRunsClient.write_scoped_memory()` uses Hermes's authenticated
`/v1/fleet/memory` API. It requires Hermes to advertise
`fleet_scoped_memory_write` and sends the same exact `fleet-memory-v1` binding
used for scoped runs.

Supported mutation shapes are:

- add;
- replace;
- remove;
- native batch operations.

Fleet's binding can never select project/network/owner/Agent-Instance as the
write scope. Shared state therefore still requires the later explicit
promotion machinery; Phase 11 does not smuggle Phase 18 promotion into normal
memory writes.

The model is not given a new permission-bearing memory toolset. Run toolsets
remain the Phase 7 exact `fleet-terminal` binding, so memory knowledge cannot
widen execution authority.

## Security properties inherited from Hermes native memory

The pinned Hermes implementation independently enforces:

- pre-prompt scope filtering;
- deterministic principal and Agent-Instance matching for private entries;
- promoted-only shared-scope visibility;
- retention expiry;
- revoked-entry invisibility;
- secret/credential-body rejection;
- fail-closed behavior when secret classification is unavailable;
- rejection of RunAuthority material in memory content;
- private filesystem ownership/mode checks;
- metadata/content consistency checks;
- scoped storage outside disposable OCI filesystems.

This preserves the architectural rule that memory can carry knowledge but
cannot carry authority.

## Persistence and isolation

Memory persistence is tied to Hermes's durable scoped memory root and the
persistent Agent Instance, not to a Run Capsule container. Destroying the OCI
body therefore does not destroy authorized memory.

Private entries additionally bind their metadata to both the owner principal
and Agent Instance. A different principal cannot retrieve them merely by using
the same Agency base or container recipe.

Project/network/owner/Agent-Instance sharing remains opt-in through explicit
promotion state and exact authorized read scopes.

## Acceptance evidence

Local Fleet validation for the Phase 11 change includes:

- 51 targeted scoped-memory/Run-Capsule/Hermes-client tests passing;
- 976 broader Fleet tests passing, 2 skipped;
- full Ruff lint passing;
- full Ruff formatting check passing;
- public repository hygiene passing;
- Desktop plugin syntax passing;
- `git diff --check` passing;
- direct Fleet-to-Hermes `fleet-memory-v1` round-trip compatibility proof
  passing against Agent commit `64094d4c8839b155f23b1969150e9197a36be941`.

The final closure gates are intentionally external to this document:

1. pull-request CI for the exact Fleet head must be green;
2. the exact merged `main` head must then have green CI.

No Phase 12 context-firewall implementation is included here.
