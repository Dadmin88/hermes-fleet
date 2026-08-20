# vNext Phase 20: Templar core

Status: **COMPLETE — MERGED AND VERIFIED ON FLEET `main`**

Phase 20 implements Templar's low-authority evaluation core on top of the exact immutable Phase 19 security-event model.

Templar may return exactly:

- `ALLOW`;
- `DENY`;
- `REVIEW`.

Those values are advisory security judgments only. They are not execution authorization, do not mutate `RunAuthority`, and cannot override a deterministic Fleet hard deny.

Phase 20 deliberately stops before the Phase 21 disposable evaluator sandbox and before the Phase 22/23 execution and learning gates.

## Ownership boundary

### Fleet owns

- constructing one exact bounded Templar evaluation request from a validated Phase 19 `SecurityEvent`;
- binding the request to the exact Phase 19 request hash and event hash;
- binding evaluator identity, model/provider/version identity, Fleet policy digest, and Templar policy identity;
- validating one closed evaluator response;
- producing one immutable authority-free Templar verdict;
- rejecting stale or request-substituted verdicts;
- failing closed when the evaluator reports timeout, fails, returns too late, or returns malformed/binding-mismatched output;
- preserving deterministic Fleet hard-deny precedence over every Templar decision.

### Templar does not own

- Fleet authorization;
- `RunAuthority` issuance or activation;
- Run Capsule lifecycle;
- node operation;
- Keryx transport;
- Nodescale trust;
- Docker or OCI lifecycle;
- arbitrary tools;
- shell or SSH;
- host-action broker authority;
- Vault bodies;
- memory/skill mutation;
- learning promotion.

The Phase 20 module has no execution, transport, Docker, Keryx, Nodescale, Vault-reader, terminal, SSH, or host-broker dependency.

## Canonical schemas

Phase 20 introduces three closed structured schemas:

- `fleet.templar-evaluation-request.v1`;
- `fleet.templar-backend-response.v1`;
- `fleet.templar-verdict.v1`.

Unknown fields are rejected rather than ignored. In particular, the backend response has no free-text rationale field, no authority field, and no place to return a command/tool request.

Evaluator output is therefore intentionally tiny:

```text
decision: ALLOW | DENY | REVIEW
reason_codes: bounded canonical codes
```

`DENY` and `REVIEW` require at least one bounded reason code. `ALLOW` may carry no reason code.

## Bounded sanitized evaluation request

The evaluator receives one structured request containing:

- exact Phase 19 request hash;
- exact Phase 19 event hash;
- exact Fleet policy digest from the security event;
- exact Templar policy reference;
- exact evaluator/model identity;
- evaluation issue/deadline timestamps;
- the exact canonical Phase 19 security-event document;
- deterministic `evaluation_id` derived from that bounded request.

The context is capped at 512 KiB and can contain only the already-validated Phase 19 security-event schema. Phase 19 already excludes secret bodies, raw memory/skill bodies, arbitrary prompt text, and authority-bearing additions.

Templar does not receive the original user prompt merely because it is evaluating security. It receives the sanitized deterministic facts assembled by Phase 19.

## Policy and evaluator identity

`TemplarPolicyRef` binds:

- policy ID;
- policy version;
- policy digest.

`TemplarEvaluatorIdentity` binds:

- evaluator ID;
- Templar implementation version;
- model provider;
- model name;
- model version.

These values are copied into every final verdict. Phase 20 therefore makes model/version/policy identity auditable without trusting the evaluator to self-assert those facts in its response.

The evaluator response carries only the evaluation/request/event identities plus the bounded decision and reason codes. Fleet supplies and owns the evaluator/policy metadata.

## Exact evaluation identity

The `evaluation_id` is the SHA-256 identity of the canonical evaluation request excluding the derived ID itself.

It binds:

- request hash;
- event hash;
- Fleet policy digest;
- Templar policy;
- evaluator/model identity;
- evaluation issue time;
- evaluation deadline;
- complete bounded security-event document.

The final verdict records the evaluation issue/deadline timestamps. During later validation Fleet reconstructs the exact evaluation request and recomputes the `evaluation_id`.

A verdict with an arbitrary hash-shaped `evaluation_id` therefore cannot pass validation merely because its other fields look plausible.

## Verdict model

A `fleet.templar-verdict.v1` record contains:

- evaluation ID;
- Phase 19 request hash;
- Phase 19 event hash;
- Fleet policy digest;
- Templar policy identity;
- evaluator/model identity;
- `ALLOW`, `DENY`, or `REVIEW`;
- bounded reason codes;
- verdict origin;
- evaluation issue/deadline timestamps;
- verdict issue/expiry timestamps;
- `authority: none`.

Verdict origins are:

- `evaluator` for a valid accepted evaluator response;
- `core-fail-closed` for a Fleet-generated fail-closed `DENY`.

A verdict carrying any authority value other than `none` is invalid.

## Fail-closed behavior

Phase 20 converts these conditions into a bounded `DENY` with origin `core-fail-closed`:

- evaluator raises `TimeoutError` -> `evaluator-timeout`;
- evaluator raises another exception -> `evaluator-failure`;
- evaluator returns after the configured monotonic/wall-clock deadline -> `evaluator-timeout`;
- response binds another request/event/evaluation -> `response-binding-mismatch`;
- malformed schema, unknown decision, missing required reason, duplicate/invalid reason codes, or unknown fields -> `malformed-response`.

The raw exception string is not copied into the verdict. This prevents backend/provider errors from becoming a new secret or private-data persistence path.

Fail-closed verdicts are restricted to the known Phase 20 reason-code set and must always be `DENY`.

### Timeout boundary with Phase 21

Phase 20 passes an exact bounded timeout to the backend, rejects explicit backend timeout errors, and rejects responses that complete after the deadline.

Phase 20 does **not** yet provide process-level hard termination if a backend implementation completely wedges and ignores the timeout contract. Phase 21 owns the fresh disposable evaluator sandbox and hard lifecycle boundary required to kill such an evaluator safely.

This distinction preserves phase ordering rather than pretending an injected in-process backend is already the Phase 21 sandbox.

## Stale verdict rejection

A verdict is valid only when all of these remain exact:

- Phase 19 request hash;
- Phase 19 event hash;
- Fleet policy digest;
- Templar policy identity/digest;
- evaluator/model identity;
- recomputed evaluation ID;
- verdict freshness window.

Changing only derived Phase 19 evidence while retaining the same request hash still changes the event hash and invalidates the verdict.

Changing the Templar policy or model version also invalidates the verdict.

A future-dated or expired verdict fails closed as stale.

## Request substitution rejection

The evaluator response must bind all three identities supplied by Fleet:

- evaluation ID;
- request hash;
- event hash.

A response that changes any one is not converted into a new evaluator judgment. Fleet produces a fail-closed `DENY` with `response-binding-mismatch`.

## Deterministic Fleet deny precedence

Phase 19 deterministic hard denies remain separate Fleet-owned objects.

`resolve_templar_disposition(...)` is a pure advisory precedence function:

1. validate the exact Templar verdict against current event/policy/evaluator state;
2. validate every supplied deterministic Phase 19 hard deny against the same event;
3. if any valid Fleet hard deny exists, return advisory `DENY` regardless of Templar output;
4. otherwise return the valid Templar decision.

This function does not authorize execution. Phase 22 still owns gate ordering and Fleet's final execution decision.

A Templar `ALLOW` therefore cannot override Fleet policy and cannot grant execution by itself.

## No-tool/no-authority interface

The Phase 20 backend contract is intentionally one method:

```text
evaluate(structured_request, timeout_ms) -> structured_response
```

Fleet supplies no tool handles, terminal, host broker, Docker client, Keryx client, Nodescale client, Vault reader, or Agent skill/memory mutation API through this core interface.

A concrete evaluator runtime with an independently enforced low-authority operating environment is Phase 21.

## Local acceptance evidence

Current Phase 20 pre-PR evidence:

- focused Phase 20 suite: 16 passed;
- Phase 19/20 security regression chain: 55 passed;
- full Fleet suite excluding the separately pinned Hermes plugin smoke: 1018 passed, 13 environment-only skips;
- exact ALLOW binding/authority-free verdict: green;
- DENY/REVIEW bounded reason validation: green;
- unsupported decision rejection: green;
- timeout and backend-failure fail-closed paths: green;
- late monotonic/wall-clock response rejection: green;
- response request-substitution rejection: green;
- unknown/free-text response-field rejection: green;
- verdict closed-schema/content-hash round trip: green;
- authority-widening verdict rejection: green;
- recomputed evaluation-ID binding: green;
- stale request/event/policy/evaluator/expiry rejection: green;
- deterministic Fleet hard deny beats Templar ALLOW, including when the accompanying verdict is stale: green;
- bounded exact Phase 19 context binding: green;
- deep-frozen stored evaluator context and backend-payload mutation isolation: green;
- full Ruff lint across `hermes_fleet`, dashboard, scripts, and tests: green;
- public-hygiene scan: green;
- Desktop plugin JavaScript syntax check: green;
- Python sdist/wheel build: green and includes `hermes_fleet/templar.py`;
- Rust `cargo fmt --all -- --check`: green;
- Rust `cargo clippy --workspace --all-targets -- -D warnings`: green;
- Rust `cargo test --workspace`: green;
- Rust `cargo build --workspace`: green;
- `git diff --check`: green.

Fleet delivery evidence is final:

- implementation PR #160 final exact head: `714d92500a5fd90161475b1d2d4f3d9d967791b1`;
- exact final-head PR CI run `32361944528`: completed successfully with Python 3.11/3.13 quality, Rust workspace compatibility, real Nodescale/readiness proofs, and the pinned-Hermes clean-install smoke all green;
- PR #160 merged normally as `b083090fa3bb98318c521ebe64153e9088f9580b`;
- exact post-merge `main` CI run `32362200150`: completed successfully on that merge SHA with all five jobs green, including the pinned-Hermes clean-install smoke;
- no force push, history rewrite, skipped failing security test, or administrative merge override was used.

Phase 20 therefore satisfies the repository closure rule end to end. The canonical next implementation entry point is Phase 21.

## Later-phase boundary

Phase 20 does **not** implement:

- Phase 21 disposable Templar sandbox;
- Phase 22 Templar pre-execution gate/order;
- Phase 23 Templar learning/promotion gate;
- Phase 26 full durable audit/provenance chain;
- Phase 35 operator Templar UI.

The next phase may wrap this exact core in a fresh low-authority disposable evaluator environment without changing what a valid Templar request or verdict means.
