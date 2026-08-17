# Hermes Fleet vNext Phase 5 acceptance: controlled host-action broker

Status: **COMPLETE**

Phase 5 introduces a narrow Fleet-owned boundary for host effects. Disposable workshops still receive no generic host power. A run may request only an operator-registered logical `(verb, target)` action whose exact parameters, budgets, authority, destination, deadline, and destination-local policy are independently validated before any new host effect begins.

This phase deliberately does not implement the formal Phase 9 principal object or Phase 10 `RunAuthority` producer early. `HostActionAuthorityScope` is the Phase 5 enforcement adapter that consumes an already-verified slice containing the exact principal ID, execution ID, RunAuthority hash, resolved Recipe hash, node-policy digest, destination digest, absolute deadline, and host-action grants. Later phases become the producers of that verified slice.

## Structured verbs only

The broker recognizes only these structured host-action verb classes:

- `deploy-approved-artifact`;
- `restart-approved-service`;
- `publish-approved-build`;
- `replace-approved-tree`;
- `query-approved-health`.

The request target is a logical operator-defined target ID, never a request-supplied host path, service-unit argument, SSH endpoint, Docker object, socket, or executable. An operator registers a `HostActionAdapterSpec` behind one exact `(verb, target)` pair. The adapter closes over the fixed host implementation. The run selects the logical action, not the implementation mechanism.

## No generic host power

The request/schema surface rejects generic authority including command/shell/argv, cwd/workdir, host/path/file-path fields, Docker, SSH, systemd/unit, socket, environment injection, host/address/endpoint/IP/port/URL/URI fields, and obvious secret-bearing fields.

Secret-like names include token, secret, password, credential, API/private/access/session keys, authorization, cookie, and JWT forms. Logical strings are bounded and reject absolute/UNC/Windows paths, traversal paths, transport URI schemes, `://` forms, and SCP-style host endpoints. Nested objects/lists are depth/count bounded and recursively revalidated.

The broker exposes no arbitrary shell, Docker socket/API, unrestricted systemd, unrestricted SSH, arbitrary host path, request-controlled environment, or request-controlled transport endpoint.

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

## Immutable structured input and evidence

Phase 5 now deep-detaches and recursively freezes request parameters after validation. Nested dictionaries/lists cannot be changed by the caller, policy callback, or another thread after authorization. The fixed adapter receives the frozen authorized structure rather than a caller-owned mutable alias.

Adapter results are likewise recursively detached and frozen before they become `HostActionEvidence`. A handler retaining its original result object cannot mutate already-issued evidence or inject later secret-bearing fields. Evidence hashes therefore remain bound to the structure that was actually validated.

## Admission order for a new effect

Before a new host effect is admitted, Fleet validates:

1. authority/request types and exact request-to-authority principal/execution/RunAuthority/Recipe binding;
2. prior idempotency outcome or in-flight state;
3. broker clock;
4. current node-policy digest against the authorized digest;
5. current resolved Recipe hash against the authorized hash;
6. canonical current destination state against the authorized target digest;
7. authority and request deadlines, including that the request cannot widen the authority deadline;
8. exact registered `(verb, target)` adapter;
9. adapter parameter schema;
10. exact parameter digest against exactly one host-action grant;
11. destination-local node-policy callback;
12. optional narrowing-only security advisory;
13. a second broker-clock/deadline check immediately before effect reservation;
14. atomic idempotency recheck plus call/rate-budget reservation.

Only then may the fixed adapter run.

Pure replay of an already-completed identical idempotency key is not a new host effect. It returns the exact stored evidence without requiring later policy, destination, clock, or deadline state to remain unchanged. A changed request using the same key is still rejected.

## Node policy and narrowing-only advisory

Every new effect requires the destination-local node-policy callback to return explicit `True`. Exceptions, unavailable policy, or any other value fail closed. The current policy digest is separately compared with the authority slice so stale authorization cannot silently survive policy changes.

The optional advisory accepts only `deny`, `review`, or `allow`:

- `deny` blocks the effect;
- `review` blocks execution pending operator review;
- `allow` grants nothing.

An `allow` advisory cannot compensate for missing authority, stale policy, changed destination, invalid parameters, expired deadline, exhausted budget, or any other deterministic denial.

## Idempotency and race-safe duplicate-effect defense

Idempotency is keyed by exact RunAuthority hash plus request idempotency key.

A completed identical retry returns the exact stored evidence and never invokes the adapter again. Reuse of the key with any changed request field is rejected. An in-flight identical key is rejected.

The in-flight/idempotency decision and budget reservation are rechecked atomically immediately before the effect. The Phase 5 concurrency regression synchronizes two identical requests so both pass the earlier admission checks before competing for the final reservation; exactly one reaches the adapter.

Call/rate budget is reserved before the adapter begins. Effect attempts consume budget even when the eventual outcome is indeterminate.

## Conservative post-effect semantics

Once the adapter begins, Fleet assumes a host effect may have occurred. The exact idempotency key becomes sticky `indeterminate` and is never re-executed when:

- the adapter raises;
- completion occurs after the authorized deadline;
- the post-effect clock is unavailable;
- the post-effect clock regresses;
- adapter evidence is malformed, unsafe, secret-bearing, or otherwise unverifiable;
- a configured audit sink cannot persist the successful effect evidence.

Post-effect clock failure cannot erase the duplicate-effect barrier: indeterminate evidence falls back to the already-recorded effect-start timestamp when necessary. Adapter exception text and rejected sensitive evidence are never copied into returned indeterminate evidence.

## Structured evidence

`HostActionEvidence` binds:

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

Evidence results are canonical JSON-compatible objects, recursively bounded and immutable after validation, and reject secret-bearing keys at any nesting depth. An optional audit sink receives successful structured evidence. If that configured sink fails, the request becomes sticky `indeterminate` instead of encouraging a retry that could duplicate the host effect. The full durable audit-chain architecture remains Phase 26.

## Real local host-effect proof

The integration proof creates an operator-configured temporary deployment target and an approved in-memory artifact store. The registered deployment adapter closes over the host deployment path, writes the approved artifact to a candidate file, and atomically replaces the fixed destination. The run request carries only logical artifact ID, artifact digest, release ID, and logical broker target.

A second registered health-query adapter inspects the same fixed deployment target. The proof verifies:

- atomic deployment succeeds;
- temporary candidate residue is absent;
- structured health evidence reflects the deployment;
- the configured host path appears in neither request nor returned evidence.

## Current proof

Local Phase 5 proof on the canonical Phase 4 base:

- focused broker unit + real local-effect suite: **22 passed**;
- complete Fleet Python suite: **874 passed**;
- repository-wide Ruff lint: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS.

The local operator guard does not permit invoking `ruff format --check`; GitHub CI remains the formatter-parity authority before merge.

No Hermes Agent change is required for Phase 5. The broker is intentionally a Fleet-local host-effect boundary. Later run/capsule phases provide the controlled invocation seam without exposing generic host authority to the workshop.

## Phase 5 closure rule

Phase 5 is canonical only when the Fleet implementation, tests, this acceptance record, and the phase-ledger update land together through the Phase 5 pull request, all required PR checks are green, the PR is merged normally, and the resulting Fleet `main` CI is green. Historical Phase 5 branches or documents do not satisfy closure by themselves.

## Explicit non-goals retained for later phases

Phase 5 does not implement:

- persistent Agent Instance orchestration: Phase 6;
- run-scoped Hermes runtime payload: Phase 7;
- Run Capsule lifecycle/recovery: Phase 8;
- formal principal identity root: Phase 9;
- complete immutable RunAuthority production/signature/replay protection: Phase 10;
- durable audit-chain ownership: Phase 26;
- Templar evaluation: later numbered phases.
