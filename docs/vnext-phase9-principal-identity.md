# Hermes Fleet vNext Phase 9 acceptance: principal identity

Status: **COMPLETE**

Phase 9 formalizes **who** a Fleet action belongs to without granting that
principal any execution authority. Identity is an input to later authorization;
it is never a substitute for `RunAuthority`.

## Identity is not authority

A principal record contains only stable identity, exact current binding evidence,
a generation fence, state, and timestamps. It does not contain:

- tool grants;
- filesystem or network grants;
- approval budgets;
- secret bodies or temporary handles;
- host-broker grants;
- resource/deadline authority;
- model/provider authority;
- Docker/runtime control state.

Changing authority-only facts must not silently change principal identity.
Prompts, model output, memories and skills cannot claim or replace a principal.

## Principal kinds

Phase 9 supports the master-plan identity kinds:

- `owner`;
- `project`;
- `network`;
- `device`;
- `service`.

`PrincipalDefinition` contains kind, stable subject and bounded scope. Its stable
`principal_id` is SHA-256 over canonical definition JSON. A current
`PrincipalReference` binds:

- stable `principal_id`;
- principal kind;
- positive generation;
- exact binding hash.

The stable ID therefore survives session/restart churn while the generation and
binding hash make rebinding/revocation visible to downstream objects.

## Durable registry and revocation

`PrincipalRegistry` is Fleet-owned durable SQLite state with:

- private owner-controlled path/file checks;
- WAL;
- `synchronous=FULL`;
- exact schema verification;
- generation-CAS transitions;
- explicit rebind only;
- explicit revocation;
- stale-reference rejection;
- malformed/tampered persisted-record failure;
- stable identity across Fleet process restarts.

A binding change is never silently accepted. `ensure()` reuses the exact current
binding or fails with an explicit rebind requirement. Rebind/revoke increments
the generation so old references become stale.

Scoped project/network/service principals bind to the exact parent principal
reference. Parent revocation/rebinding therefore invalidates the child
transitively. Self/cyclic derivation fails closed.

## Local principal resolution

Same-machine work does **not** involve Nodescale or Keryx merely to manufacture
identity.

`LocalControlServer` already authenticates Linux Unix-domain clients using
`SO_PEERCRED`. Phase 9 preserves that kernel authentication and exposes the exact
peer UID in a request-scoped `ContextVar`. The UID is available only while the
authenticated request dispatches and is restored afterward.

`LocalPrincipalResolver` maps the exact allowed UID plus local machine identity
to the durable owner principal. JSON cannot select or forge this identity and a
wrong UID is rejected before request-body dispatch.

## Remote principal resolution

Remote identity composes existing trust roots rather than inventing another
authentication protocol:

1. Keryx provides the authenticated inter-machine sender `peer_id`.
2. Nodescale's operator API must show the selected durable device is active,
   trusted, not revoked, has an active provider binding, has an applied Fleet
   projection, and has an active Keryx binding whose
   `verified_keryx_peer_id` exactly matches that authenticated sender.
3. Nodescale's strict observation API must independently show a healthy
   reconciliation and one exact online, active, unexpired provider-node
   observation matching the operator device's provider identity.
4. Fleet's durable managed projection must independently show the same exact
   active device. Its projection/membership/binding generations must match the
   operator device's Fleet-projection/credential/Keryx-binding generations.
5. Fleet admits/reuses the device `PrincipalReference` only when all four trust
   surfaces agree.

The caller supplies only the stable Nodescale device selector; it does not get to
claim the Keryx peer, network/provider identity, observation ID, trust revisions
or generations. Those facts are derived from the trusted surfaces above.

The durable principal binding contains identity/trust facts such as Keryx peer,
Nodescale device/network/provider identity, exact observation ID, durable trust
revision, provider-binding revision, Keryx binding ID/revision, and identity
binding generations. It deliberately does **not** bind Fleet
`allowed_operations` or Fleet projection generation; changing only those
authority-plane facts does not churn the principal. Phase 10 will bind authority
separately.

Remote uncertainty fails closed. Offline/expired/revoked/ambiguous Nodescale
observations, unhealthy reconciliation, sender mismatch, projection mismatch or
generation mismatch cannot resolve a current remote principal.

## Concurrent principals

Principal state is not a process-global singleton. Request-local peer context is
`ContextVar` based, and the durable registry admits distinct principals under
SQLite transaction/generation fencing. Deterministic concurrent tests prove two
owner principals remain distinct and independently current.

## Run Capsule binding

Phase 9 upgrades the persisted Run Capsule spec from
`fleet.run-capsule-spec.v2` to `fleet.run-capsule-spec.v3`.

The old opaque `principal_id` field becomes the exact typed
`PrincipalReference`. Capsule replay identity therefore includes principal kind,
generation and binding hash in addition to the stable principal ID. Old v2
`principal_id` persisted shapes fail closed rather than being silently upgraded.

For a **new** local execution, `LocalRunCapsuleExecutor.execute_initial()` checks
that exact principal reference against `PrincipalRegistry` before Capsule
admission. A stale or revoked reference therefore creates no Capsule row, Agent
Instance side effect, disposable body or Hermes run.

An already-admitted Capsule remains recoverable for exact existing-run
reconciliation and cleanup. Phase 9 does not use identity revocation as a partial
execution-cancellation mechanism; Phase 10 owns temporary `RunAuthority`
cancellation/revocation. This preserves Phase 8's no-orphan cleanup invariant.

## Explicit later-phase ownership

Phase 9 does not implement:

- temporary grants or signed/immutable `RunAuthority`: Phase 10;
- memory/skill/Vault authorization using the principal: later scoped-state
  phases;
- active-run authority cancellation/revocation: Phase 10;
- multi-user adversarial end-to-end suite: Phase 27/34;
- operator identity UX: Phase 35;
- migration/removal of legacy `requester` execution-package metadata: Phase 37.

Legacy transport metadata may continue to exist for the legacy execution path;
it is not the vNext principal model.

## Current local proof

Before PR/CI closure, Phase 9 local evidence includes:

- stable local owner identity across registry reopen;
- exact UID rejection and request-scoped kernel peer propagation;
- explicit rebind/revoke generation fencing;
- transitive parent/child invalidation and cycle rejection;
- persisted-record tamper failure;
- exact Keryx + Nodescale + managed-projection remote identity composition;
- remote offline/unhealthy/mismatch denial;
- authority-only projection change does not churn identity;
- concurrent distinct-principal isolation;
- Run Capsule v3 principal round-trip and old-v2 failure;
- revoked principal blocks initial execution before all side effects;
- existing Phase 8 Capsule/Docker lifecycle remains green with the new binding.

Current local implementation proof before PR:

- focused Phase 9 principal/local-control/Capsule/executor/Docker slice:
  **43 passed**;
- full Fleet Python suite on the verified Hermes environment:
  **950 passed, 1 skipped**;
- independent CPython 3.11.15 quality-style suite with the plugin-install smoke
  excluded: **949 passed, 1 skipped**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS.

PR **#145** implementation head
`0dcfb4b6aa6c935ac3706929dfcb447e51897922` then passed CI run
`32146927718` completely:

- Rust workspace compatibility: PASS;
- Real Nodescale and readiness proofs: PASS;
- Quality Python 3.11: PASS;
- Quality Python 3.13: PASS;
- Hermes plugin clean-install smoke: PASS;
- clean-install complete Fleet suite: **939 passed, 12 skipped**.

The closure-status commit intentionally changes the PR head after that proof. Per
repository policy, fresh CI must pass on the new exact head before merge, and the
resulting Fleet `main` merge commit must then pass its push CI. The `COMPLETE`
label is not itself closure evidence.

## Closure gates

Phase 9 is complete only after:

1. focused principal/concurrency/revocation/Capsule tests pass;
2. full Fleet Python tests pass;
3. full Ruff, `git diff --check`, formatting and public-hygiene checks pass;
4. Python 3.11 compatibility is independently exercised;
5. exact PR-head CI is green, including Hermes clean-install smoke;
6. the PR merges normally without history rewriting;
7. resulting Fleet `main` push CI is green on the exact merge commit.

Phase 10 remains locked until those gates close.
