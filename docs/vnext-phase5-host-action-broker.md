# Hermes Fleet vNext Phase 5 acceptance: controlled host-action broker

Status: **COMPLETE**

Phase 5 introduces a narrow Fleet-owned boundary for host effects. Disposable workshops still receive no generic host power. A run may request only an operator-registered logical `(verb, target)` action whose exact parameters, budgets, authority, destination, deadline, and node policy are independently validated before any host effect begins.

This phase deliberately does not implement the full Phase 9 principal object or Phase 10 `RunAuthority` object early. `HostActionAuthorityScope` is the Phase 5 enforcement adapter that consumes an already-verified slice containing the exact principal ID, execution ID, RunAuthority hash, resolved Recipe hash, policy digest, destination digest, deadline, and host-action grants. Later phases become the producers of that verified slice.

## Structured verbs only

The broker recognizes only these structured host-action verb classes:

- `deploy-approved-artifact`;
- `restart-approved-service`;
- `publish-approved-build`;
- `replace-approved-tree`;
- `query-approved-health`.

The request target is a logical operator-defined target ID, not a host path, service-unit argument, SSH endpoint, or Docker object.

An operator registers a `HostActionAdapterSpec` behind one exact `(verb, target)` pair. The adapter declares required/optional logical parameter names and closes over whatever fixed host implementation is appropriate for that target. A request cannot supply code, executable choice, or host addressability.

## No generic host power

The broker request surface categorically rejects parameter schema/key/value forms that expose generic authority, including command/shell/argv, host paths/cwd, Docker, SSH, systemd/unit, sockets, environment injection, or obvious secret-bearing fields.

Logical string values are bounded and may not contain absolute host paths or transport URIs. Nested parameter objects/lists are depth/count bounded and are revalidated recursively.

The broker itself exposes no:

- arbitrary shell;
- Docker socket or generic Docker API;
- unrestricted systemd;
- unrestricted SSH;
- arbitrary host path;
- request-controlled environment injection.

Host code may exist only behind a fixed operator-registered adapter. The run selects the logical action, not the implementation mechanism.

## Authority grant model

Each `HostActionGrant` binds exactly:

- one structured verb;
- one logical target;
- the canonical parameter digest;
- maximum call attempts;
- maximum attempts per minute.

`HostActionAuthorityScope` binds those grants to:

- exact principal ID;
- exact execution ID;
- exact RunAuthority hash;
- exact resolved Recipe hash;
- exact current node-policy digest;
- exact destination-target digest;
- absolute authority deadline.

A request cannot widen any of these values.

## Validation order

Before a new host effect is admitted, Fleet validates:

1. request/scope types and broker clock;
2. exact principal identity string;
3. exact execution identity;
4. exact RunAuthority hash;
5. exact resolved Recipe hash;
6. current node-policy digest against the authorized digest;
7. current resolved Recipe hash against the authorized hash;
8. canonical current destination state against the authorized target digest;
9. prior idempotency outcome, if any;
10. authority and request deadlines;
11. exact registered `(verb, target)` adapter;
12. adapter parameter schema;
13. exact parameter digest against exactly one host-action grant;
14. current node-policy callback;
15. optional security advisory, which may only narrow;
16. call-attempt budget and rate limit.

Only then may the adapter run.

## Node policy

The broker requires a destination-local node-policy callback for every new effect. Exceptions, unavailable policy, or any value other than explicit `True` fail closed.

The current policy digest is also separately compared to the authority slice, preventing a stale authorization object from being reused after policy changes.

## Idempotency and duplicate-effect defense

Idempotency is keyed by the exact RunAuthority hash plus request idempotency key.

A completed request:

- returns the exact stored evidence on an identical retry;
- rejects the same idempotency key if any request field changes;
- does not run the adapter again.

An in-flight identical key is rejected rather than executed twice.

Call/rate budget is reserved **before** the adapter begins. This makes the limit race-safe across concurrent distinct idempotency keys. The concurrency regression uses `max_calls=1`, blocks the first adapter in-flight, and proves a second call cannot slip through while the first is still running.

## Conservative failure semantics

Once an adapter begins, Fleet assumes a host effect may have occurred.

If the adapter:

- raises an exception;
- completes after the authorized deadline; or
- returns malformed/unverifiable/sensitive evidence;

Fleet stores a structured `indeterminate` evidence record for that idempotency key and raises `HostActionIndeterminateError`.

A retry of the same idempotency key returns the same sticky indeterminate outcome and **never re-executes the adapter**. This is intentionally conservative: ambiguity is not permission to duplicate a deployment, service restart, publish, or tree replacement.

Effect attempts consume call/rate budget even when their final outcome is indeterminate.

## Structured evidence

Successful and indeterminate outcomes use `HostActionEvidence` bound to:

- status;
- exact request hash;
- exact RunAuthority hash;
- principal ID;
- execution ID;
- resolved Recipe hash;
- verb;
- logical target;
- idempotency key;
- start/completion timestamps;
- canonical result hash;
- bounded structured result.

Evidence results are canonical JSON objects and reject obvious secret-bearing result keys. Adapter exception text is not copied into indeterminate evidence.

An optional audit sink receives the structured evidence after a successful effect. Full durable audit-chain ownership remains Phase 26.

## Templar/advisory boundary

Phase 5 does not implement Templar early, but it locks the authority rule Templar must later obey.

The broker accepts an optional advisory value:

- `deny` blocks the effect;
- `review` blocks execution and requires operator review;
- `allow` grants nothing.

An `allow` advisory cannot compensate for a missing host-action grant, stale policy, changed target, invalid parameters, exhausted budget, or any other deterministic denial.

This gives later Templar integration a narrowing-only seam without granting it broker authority.

## Real local host-effect proof

The integration proof creates an operator-configured temporary deployment target and an approved in-memory artifact store.

The registered deploy adapter closes over the deployment path, writes an approved artifact to a candidate file, and atomically replaces the fixed destination. The run request carries only:

- logical artifact ID;
- artifact digest;
- release ID;
- logical broker target.

A second registered health-query adapter inspects the same fixed deployment target.

The proof verifies:

- atomic deployment succeeds;
- temporary candidate residue is absent;
- structured health evidence reflects the deployment;
- the configured host path appears in neither request nor returned evidence.

## Tests and current proof

Phase 5 current proof:

- focused broker unit + real local-effect suite: **15 passed**;
- full Fleet Python suite: **813 passed**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS.

No Hermes Agent changes are required in Phase 5. The broker is intentionally a Fleet-local host-effect boundary. Later run/capsule phases will provide the controlled invocation seam without exposing generic host authority to the workshop.

## Explicit non-goals retained for later phases

Phase 5 does not implement:

- persistent Agent Instance orchestration: Phase 6;
- run-scoped Hermes runtime payload: Phase 7;
- Run Capsule lifecycle/recovery: Phase 8;
- formal principal identity root: Phase 9;
- complete immutable/signed RunAuthority issuance and replay protection: Phase 10;
- Templar evaluation itself: Phases 19–23;
- final durable audit/provenance chain: Phase 26.

The broker is ready to consume those later identities and policies without granting them early.
