# vNext Phase 22: Templar pre-execution gate

Status: **CLOSURE-GATED**

Phase 22 composes the security components delivered in Phases 19–21 into the mandatory vNext pre-execution ordering. It does not give Templar authority and it does not implement the Phase 23 learning/promotion gate.

The required order is now represented directly in code:

```text
authenticated request
    -> deterministic Fleet policy
    -> destination admission
    -> Templar if policy requires it
    -> Fleet final decision
    -> RunAuthority activation
    -> exact Run Capsule
    -> execution
```

The critical invariant is that no disposable execution body may be created unless the exact request has reached the end of this sequence successfully.

## Delivered boundary

Phase 22 adds `hermes_fleet.pre_execution_gate` and requires a Phase 22 `PreExecutionPermit` at the initial Run Capsule execution boundary.

The gate consumes:

- one authenticated `PrincipalReference`;
- one proposed immutable `RunAuthority` document that is not yet execution power;
- the exact Phase 19 `SecurityEvent` derived from that proposed authority;
- deterministic Fleet policy output;
- destination-admission evidence;
- the Phase 20 `TemplarCore`, backed by the Phase 21 disposable sandbox when policy requires Templar;
- a Fleet final-decision callback;
- current principal/Agent/Recipe/policy/capabilities/target context;
- the durable `RunAuthorityStore`.

A successful gate returns:

- the exact derived `RunCapsuleSpec`;
- a short-lived, content-bound `PreExecutionPermit` carrying `authority: none`.

The permit is evidence that the gate completed. It is not a second source of execution authority. `RunAuthority` remains Fleet's single root of temporary execution power.

## Authenticated request binding

`fleet.pre-execution-request.v1` binds:

- operation `fleet.hermes.run`;
- exact authenticated principal reference;
- proposed RunAuthority content hash;
- exact Phase 19 security event.

Construction fails if the authenticated principal differs from the RunAuthority principal, if the security event does not bind the proposed authority, or if the event's principal/policy/capabilities/target facts differ from the proposed authority.

The proposed RunAuthority document may exist before the gate because its content hash is required to construct the Phase 19 security request. It remains identity-only at that point. The durable `RunAuthorityStore` is not activated until after the Fleet final decision is `ALLOW`.

## Deterministic Fleet policy runs first

`fleet.pre-execution-policy.v1` binds the deterministic policy result to:

- Phase 22 gate-request hash;
- Phase 19 request hash;
- Phase 19 event hash;
- current Fleet policy digest;
- whether Templar is required;
- zero or more exact Phase 19 deterministic hard-deny records.

Hard-deny records are revalidated against the exact event before use.

If at least one deterministic hard deny is present:

- the gate returns `DENY` immediately;
- destination admission is not invoked;
- Templar is not invoked;
- Fleet final decision is not invoked;
- no RunAuthority is activated;
- no Run Capsule or body is created.

This preserves the rule that an AI evaluator never gets an opportunity to debate or override a deterministic Fleet deny.

## Destination admission precedes Templar

`fleet.destination-admission.v1` is immutable evidence bound to:

- gate-request hash;
- Phase 19 request/event hashes;
- exact target digest;
- `admitted` or `DENY` status;
- bounded deterministic reason codes.

A stale, substituted, failed, or denied destination-admission result stops the request before Templar runs.

Phase 22 deliberately models destination admission as an injected Fleet/destination-local evaluator rather than activating the legacy FX8 execution path. The later Phase 37 migration remains responsible for retiring the legacy disposable-profile execution route and activating the final production-local execution shape. Phase 22 does not silently pull that migration forward.

## Templar is conditional and still low-authority

If deterministic policy says Templar is not required, the gate skips Templar and proceeds to Fleet's final decision.

If Templar is required:

- missing Templar fails closed;
- the exact immutable Phase 19 event is passed to `TemplarCore`;
- the verdict is revalidated against the exact event, Templar policy, evaluator/model identity, and current time;
- `DENY` stops immediately;
- `REVIEW` is routed to the configured operator-review flow and stops execution;
- `ALLOW` merely permits the request to reach Fleet's final decision.

Templar `ALLOW` is never transformed into execution authority.

## REVIEW routes operator flow

A Templar `REVIEW` verdict invokes the configured review router with the exact Phase 22 request and exact Templar verdict.

The gate returns a `REVIEW` outcome containing the bounded operator-review reference and no execution material.

If review routing is unavailable or fails, the gate fails closed to `DENY`.

No RunAuthority is activated on either `REVIEW` path.

## Fleet final decision remains authoritative

`fleet.pre-execution-final-decision.v1` binds Fleet's final decision to:

- gate-request hash;
- Phase 19 request/event hashes;
- Fleet policy digest;
- exact destination-admission hash;
- exact Templar verdict hash when Templar ran;
- `ALLOW` or `DENY`;
- bounded reason codes.

A Templar `ALLOW` followed by Fleet `DENY` stops the request. This is explicitly covered by the Phase 22 tests.

Only Fleet final `ALLOW` can advance to RunAuthority activation.

## Stale-request invalidation

The gate captures current mutable context before deterministic policy:

- principal reference/generation/binding;
- Agent Instance id;
- Recipe and ResolvedRecipe hashes;
- Fleet policy digest;
- capabilities hash;
- target digest;
- current provider/model constraints when applicable.

The proposed RunAuthority is validated against that context before policy evaluation.

After Templar and Fleet final `ALLOW`, the context is fetched again. Any change invalidates the request before RunAuthority admission.

Immediately after RunAuthority admission the context is fetched and validated a third time. If a mutation raced the authority-store commit, Fleet cancels the newly admitted RunAuthority and issues no pre-execution permit.

The local Run Capsule executor then performs its existing independent RunAuthority/current-principal/policy/capabilities/target checks again before claiming the Capsule or creating a body.

This gives three fail-closed layers around the mutable-state race window:

1. pre-gate/current-context validation;
2. pre-authority and post-authority gate validation;
3. executor-side current-authority validation before Capsule/body lifecycle work.

## RunAuthority activation occurs only after final ALLOW

The gate does not call `RunAuthorityStore.admit()` until:

- deterministic policy has no hard deny;
- destination admission succeeded;
- required Templar evaluation returned `ALLOW`;
- Fleet final decision returned `ALLOW`;
- current request context still matches the proposed immutable authority.

If authority admission fails, the request fails closed and no permit is issued. Immediately after admission Fleet revalidates the exact current context. Any post-admission failure before permit issuance, including Capsule derivation, expiry-window collapse, or permit sealing failure, cancels the newly admitted RunAuthority and returns `DENY` with no permit.

The exact `RunCapsuleSpec` is then derived from the admitted immutable RunAuthority.

## Pre-execution permit

`fleet.pre-execution-permit.v1` binds:

- Phase 22 gate-request hash;
- Phase 19 request/event hashes;
- Fleet policy digest;
- exact RunAuthority hash;
- exact Run Capsule hash;
- Fleet final-decision hash;
- exact Templar verdict hash when Templar ran;
- issue and expiry timestamps;
- a Fleet HMAC-SHA256 issuance seal;
- `authority: none`.

Permit TTL is bounded and may never outlive the RunAuthority/Run Capsule deadline. The seal is computed over the complete unsigned permit document using one process-local Fleet sealing key shared by the gate and the permit-enforced executor. The key never enters the permit or evaluator. The seal proves only that this exact permit was issued by the configured Fleet gate; it does not create execution authority and cannot replace the active RunAuthority checks.

A permit fails validation if:

- it is not yet valid or has expired;
- the RunAuthority hash changed;
- the Run Capsule content hash changed;
- its lifetime exceeds the Capsule deadline;
- its Fleet issuance seal does not verify.

A restart before initial Capsule admission requires a fresh gate/permit because the sealing key is intentionally process-local. Recovery of an already admitted Capsule continues through the existing exact RunAuthority/Run Capsule recovery path rather than treating a permit as durable authority.

The permit does not contain grants, secrets, node-control power, provider credentials, or a generalized authorization token.

## No Run Capsule body before gate success

`LocalRunCapsuleExecutor.execute_initial()` now requires an exact `PreExecutionPermit` plus the matching Fleet `PreExecutionPermitSealer`. The executor verifies the HMAC issuance seal and exact Capsule binding before any lifecycle mutation.

Permit validation occurs before:

- principal lookup effects relevant to execution;
- RunAuthority Capsule claim;
- Run Capsule store admission;
- Agent Instance preparation;
- runtime-material/secret binding;
- body creation;
- Hermes run submission.

A stale/substituted permit therefore leaves the Capsule store empty and the body factory untouched.

After permit validation, the executor still requires the exact active RunAuthority and current principal/policy/capabilities/target state, claims the exact Capsule identity, admits the Capsule, and only then creates the disposable body.

This is the concrete Phase 22 enforcement of "no container before gate succeeds."

## Phase 22 does not activate the legacy FX8 path

The repository still contains the older `DestinationRecipeExecutor`/execution-package route that materializes execution-owned Hermes state directly after its legacy destination admission. The vNext ledger already tracks migration off that route in Phase 37.

Phase 22 does not claim that legacy route is now the final production execution path, and it does not weaken the Phase 37 requirement. The mandatory permit is enforced at the vNext `LocalRunCapsuleExecutor` initial-execution boundary that later production activation must use.

## Failure semantics

Security uncertainty fails closed.

The gate converts policy/admission/Templar/final-decision/context/authority-admission uncertainty into bounded `DENY` outcomes. Raw exception text is not included in the gate result.

`REVIEW` is the only non-deny outcome that stops without failure, and it carries no RunAuthority activation or execution permit.

## Local acceptance evidence

Current Phase 22 pre-PR evidence:

- focused Phase 22 gate suite: 19 passed;
- existing Run Capsule executor suite after mandatory sealed-permit integration: 23 passed;
- Phase 19–22 security/lifecycle regression chain: 108 passed, 6 explicit Bubblewrap skips;
- deterministic hard deny short-circuits destination admission, Templar, Fleet final decision, and authority activation: green;
- destination denial occurs before Templar: green;
- exact happy-path order `context -> policy -> destination -> Templar -> Fleet final -> context -> RunAuthority -> context`: green;
- Templar `DENY` stops: green;
- Templar `REVIEW` routes operator flow without authority: green;
- missing review router fails closed: green;
- Templar `ALLOW` cannot override Fleet final `DENY`: green;
- policy may explicitly skip Templar while Fleet final decision remains mandatory: green;
- missing required Templar fails closed: green;
- policy/principal/target mutation after verdict invalidates the request before authority: green;
- mutation racing RunAuthority admission cancels the admitted authority and issues no permit: green;
- stale destination-admission binding fails closed before Templar: green;
- authenticated-principal mismatch fails at request construction: green;
- permit is authority-free, exact-Capsule bound, and Fleet-sealed: green;
- forged/tampered permit seal blocks Capsule admission and body creation before side effects: green;
- permit issuance failure after RunAuthority activation cancels that authority and issues no permit: green;
- real Phase 22 gate output is accepted by the sealed-permit Run Capsule executor and completes the fake lifecycle: green;
- stale permit blocks Capsule admission and body creation before side effects: green;
- explicit real Phase 21 Bubblewrap regression under the Phase 22 tree: 14 passed;
- full Fleet suite excluding the separately pinned Hermes plugin smoke: 1048 passed, 18 environment/integration skips on the final rerun;
- local real-Docker Run Capsule lifecycle proof: one explicit skip because the pinned amd64 image is unavailable locally;
- full Ruff lint across `hermes_fleet`, dashboard, scripts, and tests: green;
- Black compatibility for all modified Python files: green;
- public-hygiene scan: green;
- Desktop plugin JavaScript syntax check: green;
- Python sdist/wheel build: green, with `hermes_fleet/pre_execution_gate.py` present in the wheel;
- Rust `cargo fmt --all -- --check`: green;
- Rust `cargo clippy --workspace --all-targets -- -D warnings`: green;
- Rust `cargo test --workspace`: green;
- Rust `cargo build --workspace`: green;
- `git diff --check`: green.

Repository closure still requires the normal path:

1. reviewable branch/commit;
2. exact final PR-head CI green;
3. normal GitHub merge;
4. exact resulting `main` CI green;
5. closure record update through the normal closure workflow.

Until those steps are complete, Phase 22 remains `CLOSURE-GATED` rather than `COMPLETE`.

## Later-phase boundary

Phase 22 does **not** implement:

- Phase 23 Templar learning/promotion gate;
- Phase 24 Agency base + learned overlay upgrades;
- Phase 26 full durable audit/provenance chain;
- Phase 35 operator Templar UX;
- Phase 37 migration/production activation of the legacy execution route.

Phase 23 may reuse the authority-free Phase 20/21 evaluator for candidate/memory promotion decisions, but it must bind those decisions to exact candidate hashes and cannot promote anything by itself.
