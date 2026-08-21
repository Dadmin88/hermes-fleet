# vNext Phase 23: Templar learning/promotion gate

Status: **IMPLEMENTED ON FEATURE BRANCH — MERGE/CI CLOSURE PENDING**

Phase 23 extends the low-authority Templar evaluator from execution admission to durable learning promotion without giving Templar promotion authority.

The canonical learning path is now:

```text
Hermes private memory / quarantined skill candidate
    -> Hermes Phase 18 prepare
       - exact source lookup
       - secret/private-data sanitation
       - Phase 17 skill verification where applicable
       - exact sanitized candidate hash
       - bounded sanitized semantic evaluation material
    -> Fleet Phase 23 learning-promotion request/event
       - exact candidate-hash reconstruction
       - principal / Agent Instance / scope / administrator binding
       - current Fleet learning-policy digest
       - deterministic semantic risk signals
    -> deterministic pre-evaluator hard stops
    -> Fleet deterministic Phase 18 promotion-policy preflight
    -> Templar ALLOW / DENY / REVIEW
    -> Fleet deterministic promotion-policy revalidation
    -> short-lived exact PromotionAuthorization
    -> Hermes Phase 18 commit
       - re-read exact source
       - re-sanitize
       - re-run Phase 17 verification for skills
       - re-check exact approved hash
       - commit durable promoted state
```

The two critical invariants are:

1. the Templar verdict is bound to the exact sanitized candidate hash that Fleet may later authorize; and
2. Templar never receives a mutation callback, promotion token, or authority-producing API.

## Hermes-side preparation contract

The existing Phase 18 `/v1/fleet/promotions/prepare` seam remains the source of promotion material. Phase 23 extends its response rather than inventing a second Agent learning format.

Hermes now advertises:

```text
fleet_learning_promotion_gate_material = true
```

Fleet requires that capability before using the Phase 23 prepare path. Mixed-version peers fail closed rather than silently promoting without semantic evaluation material.

Prepared memory material is emitted as:

```json
{
  "schema": "fleet.promotion-evaluation-material.v1",
  "kind": "memory",
  "content_hash": "sha256:<exact sanitized text hash>",
  "bytes": 123,
  "text": "<sanitized text>"
}
```

Prepared skill material is emitted as a bounded list of sanitized textual files. Every file carries its relative path, byte length, exact SHA-256 digest, and sanitized text. The overall `content_hash` remains the existing Phase 18 canonical manifest digest over `{path, sha256, bytes}` entries.

Hermes refuses semantic material larger than 256 KiB. It does not truncate oversized candidates because truncation could hide the very text Templar is meant to evaluate.

No secret body, RunAuthority, network grant, filesystem grant, host permission, or execution authority is added to the prepare response.

## Fleet independently reconstructs the candidate hash

`hermes_fleet.learning_promotion_gate` does not trust a prepared response merely because Hermes supplied a hash string.

For memory candidates Fleet:

- validates the closed material schema;
- validates the declared byte count;
- hashes the exact sanitized UTF-8 text independently;
- requires that digest to equal the prepared `approved_content_hash`.

For skill candidates Fleet:

- validates every relative path;
- rejects traversal/absolute paths;
- validates every file byte count and SHA-256 digest against the supplied sanitized text;
- requires unique, sorted paths;
- requires `SKILL.md`;
- reconstructs the Phase 18 manifest from `{path, sha256, bytes}`;
- independently hashes that canonical manifest;
- requires the result to equal the prepared `approved_content_hash`.

A changed byte therefore changes the candidate identity before Templar runs.

Fleet caps accepted evaluation material below the existing Phase 20/21 Templar context bounds, so one valid Phase 23 event remains admissible to the disposable evaluator sandbox.

## Exact Phase 23 request/event binding

`fleet.learning-promotion-request.v1` binds:

- subject kind and subject key;
- source owner principal;
- Agent Instance;
- source scope;
- target scope;
- exact source content hash;
- exact approved/candidate content hash;
- exact authenticated administrator reference;
- current Fleet learning-policy digest;
- sanitation state;
- exact semantic evaluation material;
- Phase 17 verification digest for skills;
- expected current promotion ID when applicable;
- `authority: none`.

`fleet.learning-promotion-event.v1` binds the complete request hash and exposes the bounded semantic-review categories to Templar.

The event is immutable and content-addressed. Phase 20 Templar verdicts now support both the original Phase 19 execution-security event and the Phase 23 learning-promotion event through the same exact request/event hash binding machinery.

Changing the candidate after evaluation invalidates the prior Templar verdict.

## Required semantic evaluation categories

Every Phase 23 learning event explicitly asks Templar to evaluate:

- hidden instructions;
- social-engineering text;
- exfiltration intent;
- disguised privilege escalation;
- dangerous combinations;
- suspicious secret handling.

Fleet also derives conservative deterministic risk signals from the sanitized text. These signals are evidence/context, not model authority and not an automatic permission mechanism.

If multiple suspicious categories co-occur, Fleet adds a `dangerous-combinations` signal.

## Secret handling fails before the evaluator

Hermes Phase 13/18 sanitation remains the primary secret/private-data boundary.

Phase 23 adds an independent last-resort pre-evaluator check for known unredacted credential forms such as private-key material, AWS access-key forms, GitHub token forms, long `sk-...` credentials, and bearer JWT forms.

If one is still present:

- the candidate is `DENY`;
- Templar is not invoked;
- no promotion authorization is created.

This prevents a sanitation failure from becoming a path that forwards an obvious live credential to the model evaluator.

The hard stop is intentionally conservative and does not replace the broader Phase 13 classifier.

## Templar remains low authority

The Phase 23 gate accepts the existing `TemplarCore`.

If Templar is unavailable when required, the learning request fails closed.

`DENY`:

- stops promotion;
- carries bounded reason codes;
- creates no `PromotionAuthorization`.

`REVIEW`:

- routes the exact request/verdict to the configured operator-review flow;
- returns only a bounded review reference;
- creates no `PromotionAuthorization`;
- fails closed to `DENY` when review routing is unavailable or fails.

`ALLOW`:

- still creates no authority by itself;
- merely permits an already-policy-eligible request to reach Fleet's final deterministic revalidation and authorization mint.

Templar has no commit function, no Agent write primitive, no memory/skill mutation callback, and no way to mint a Fleet promotion authorization.

## Deterministic Fleet policy runs before and after Templar

Phase 23 splits the existing Phase 18 promotion policy into a pure `validate_promotion_policy()` preflight plus the existing `authorize_promotion()` minting primitive.

Before Templar runs, the pure preflight enforces:

- exact target-scope administrator control;
- private-source ownership/delegation rules;
- monotonic scope widening;
- exact source and approved hashes;
- exact Phase 17 verification digest for skills;
- bounded authorization lifetime parameters;
- authority-free promotion semantics.

If deterministic Fleet promotion policy rejects the request, the Phase 23 result is `DENY`, Templar is never invoked, and no promotion authorization exists even transiently.

After an exact Templar `ALLOW`, Fleet calls `authorize_promotion()`. That primitive re-runs the same deterministic policy and only then mints the short-lived exact `PromotionAuthorization`.

This is the learning equivalent of the Phase 22 rule that deterministic Fleet policy runs first and an evaluator can never debate or overrule a deterministic deny.

## Deterministic skill verification still runs

Phase 23 does not replace or weaken Phases 16–18.

A skill reaches the gate only after the existing Hermes prepare path:

- requires the candidate to remain quarantined/inactive;
- verifies the exact candidate binding;
- re-runs Phase 17 verification;
- carries the exact verification digest into the Phase 23 request.

After Fleet eventually issues a valid promotion authorization, Hermes commit re-runs preparation and Phase 17 verification again before materializing promoted state.

Therefore Templar semantic review is an additional gate around deterministic verification, not a substitute for it.

## Hermes client fail-closed compatibility

`HermesRunsClient.prepare_memory_promotion()` and `prepare_skill_promotion()` now require both:

- `fleet_learning_promotion`; and
- `fleet_learning_promotion_gate_material`.

They also reject prepare responses that omit `evaluation_material`.

Commit/history/rollback operations retain the base Phase 18 capability contract because those operations do not need to expose semantic material.

This keeps the Agent seam additive and mixed-version behavior explicit.

## Local acceptance evidence

Current pre-merge evidence:

- Hermes canonical isolated promotion/API regression: **19 passed, 0 failed**;
- Hermes full Ruff lint: green, with one pre-existing malformed `noqa` warning in `run_agent.py`;
- Hermes `git diff --check`: green;
- Fleet Phase 18–23 focused security/lifecycle regression: **115 passed, 6 explicit sandbox skips**;
- complete Fleet suite using the exact Phase 23 Hermes checkout on PATH: **1060 passed, 18 environment/integration skips**;
- Fleet full Ruff lint: green;
- exact memory candidate-hash reconstruction: green;
- exact skill manifest/text candidate-hash reconstruction: green;
- changed candidate invalidates prior verdict: green;
- Templar `DENY` emits no promotion authorization: green;
- Templar `REVIEW` emits no promotion authorization: green;
- missing Templar fails closed: green;
- stale Fleet learning policy fails closed: green;
- authenticated administrator substitution fails closed: green;
- obvious unredacted credential material is stopped before evaluator invocation: green;
- all six required semantic-risk categories are represented and exercised: green;
- deterministic Fleet promotion-policy denial short-circuits before Templar: green;
- Fleet revalidates deterministic promotion policy after Templar `ALLOW`: green;
- skill promotion without exact Phase 17 verification evidence is rejected: green.

Hermes delivery evidence:

- implementation branch: `feat/phase23-learning-promotion-gate`;
- implementation commit: `bc9b726367`;
- PR: Dadmin88/hermes-agent #17;
- exact-head CI run `32500586143`: green, including all 12 Python test slices, blocking Ruff checks, OS-specific checks, OSV, and the supply-chain review gate;
- PR #17 merged normally as `9f81755f825515821bcdd3a7f00e5adf13717224`;
- exact post-merge `main` CI run `32501665019`: attempt 1 hit an unrelated shared-metrics SQLite `database is locked` concurrency flake; failed jobs were rerun on the same merged SHA and attempt 2 completed green.

Fleet CI is pinned to the exact merged Hermes revision `9f81755f825515821bcdd3a7f00e5adf13717224` and includes an explicit clean-install Phase 23 Agent/Fleet promotion-seam test.

## Later-phase boundary

Phase 23 does **not** implement:

- Phase 24 Agency base + learned overlay upgrade mechanics;
- Phase 25 right-to-forget/revocation cleanup;
- Phase 26 full durable audit/provenance chain;
- Phase 27 multi-principal adversarial system suite;
- Phase 35 operator learning-review UX;
- Phase 37 activation/migration off legacy disposable-profile execution.

Phase 24 may now rely on one explicit rule: learned state cannot broaden scope through the normal Fleet promotion path without exact sanitized candidate binding, required Templar evaluation, and deterministic Fleet authorization.
