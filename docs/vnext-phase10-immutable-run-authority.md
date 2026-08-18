# Hermes Fleet vNext Phase 10 acceptance: immutable RunAuthority

Status: **IN PROGRESS — implementation pending PR/main CI**

Phase 10 makes RunAuthority the single immutable root of temporary execution
power. A RunAuthority is an exact content-addressed authorization document; its
operational lifecycle state is stored separately so cancellation or revocation
never rewrites the authorized request.

## Ownership boundary

RunAuthority is Fleet-owned authorization state. It is not:

- persistent Agent state;
- prompt/model output;
- a memory or skill;
- a container configuration escape hatch;
- Keryx transport identity;
- Nodescale device identity;
- a Templar verdict.

Principal identity comes from Phase 9. Recipe requirements come from Phase 8A.
RunAuthority binds those already-resolved facts to one exact permitted run.

## Immutable authority document

`fleet.run-authority.v1` binds:

- execution ID and idempotency digest;
- exact `PrincipalReference`;
- persistent Agent Instance ID;
- logical Recipe and exact ResolvedRecipe hashes;
- Recipe compiler/provenance and optional Workflow-step identity;
- digest-pinned runtime image;
- current policy digest;
- current capability hash;
- exact destination and destination digest;
- exact execution-plan fingerprint;
- issuance/deadline timestamps;
- CPU, RAM, PID and model-iteration limits;
- mandatory OCI isolation posture;
- network requirements;
- filesystem projections;
- artifact exports;
- toolsets;
- approval budget;
- symbolic secret references only;
- structured host-action grants;
- optional model/provider constraints;
- project scope;
- remote Keryx task identity when applicable.

The canonical JSON bytes produce the RunAuthority `content_hash`. A separate,
domain-separated `audit_hash` is also produced for later provenance/audit use.

## Grant derivation, not circular authority hashes

Existing Fleet network and filesystem grant objects already require an exact
`authority_ref`. Embedding those self-referential objects in RunAuthority would
create a circular hash.

Phase 10 therefore stores **unbound authority intents** inside RunAuthority and
materializes existing bound grant objects only after the RunAuthority hash is
known:

```text
immutable RunAuthority intent
    -> RunAuthority content hash
    -> authority-bound NetworkGrant
    -> authority-bound FilesystemGrant(s)
    -> HostActionAuthorityScope
    -> exact RunCapsule projection
```

The resulting Capsule must equal `RunAuthority.to_capsule_spec()` exactly.
Changing a toolset, grant, resource limit, principal, Recipe, deadline or any
other authority-bearing field makes the Capsule invalid for that authority.

## Signatures / attestations

`RunAuthoritySigner` provides an HMAC-SHA256 attestation over the exact
RunAuthority hash using a host-supplied key. The key is never persisted by the
RunAuthority store.

The attestation binds:

- exact authority hash;
- signer key ID;
- algorithm.

Changed authority content or a different signing key fails verification.

## Durable operational state

`RunAuthorityStore` persists the immutable authority document plus separate
operational state:

- `active`;
- `cancelled`;
- `revoked`.

The state record also carries:

- monotonic state generation;
- one exact claimed Run Capsule hash;
- creation/update timestamps;
- immutable audit hash.

A pre-execution narrowing transition preserves the parent immutable document as
`superseded` and atomically activates the narrower child authority. The child
keeps the same execution/idempotency identity and records the exact parent hash.
Direct reuse of those identities by an unrelated authority remains a replay
conflict. Narrowing is forbidden after a Capsule has claimed the authority.

Cancellation/revocation changes only this state record. It never rewrites the
RunAuthority JSON or content hash.

The store uses private ownership/mode checks, no-follow creation,
non-symlink-directory-component validation, SQLite WAL, `synchronous=FULL`, and
closed-schema/tamper validation on reads.

## Expiry and validity

A RunAuthority is usable only when:

```text
issued_at_ms <= now < deadline_ms
```

An already-expired or not-yet-valid authority cannot be admitted. Exact deadline
is expired, not one final usable millisecond.

## Replay protection

The durable store binds one authority to:

- authority hash;
- execution ID;
- idempotency digest.

A changed authority cannot reuse an existing execution ID or idempotency digest.

One authority may claim exactly one canonical Run Capsule. Exact concurrent
replay converges on the same claim; changed Capsule material is rejected.

## Current-context validation

Before execution, Fleet revalidates:

- exact current principal reference;
- Agent Instance ID;
- logical Recipe hash;
- ResolvedRecipe hash;
- current policy digest;
- current capabilities hash;
- destination digest;
- deadline;
- model/provider when constraints are present.

Any stale fact fails closed.

## Monotonic narrowing

A child authority may reduce, never increase, parent power.

Narrowing can reduce:

- deadline;
- CPU/RAM/PID/iteration limits;
- network destinations or collapse network to `none`;
- filesystem projections/byte limits;
- artifact exports/byte limits;
- toolsets;
- approval budget;
- symbolic secret references;
- host-action grants/call/rate limits;
- model/provider sets;
- project scope.

It cannot change the principal, Agent Instance, Recipe, policy, capabilities,
destination, isolation baseline or other immutable identity facts. The child
records `parent_authority_hash`.

## Run Capsule execution enforcement

`LocalRunCapsuleExecutor` now requires both:

- a current Phase 9 principal;
- a current exact RunAuthority.

It checks authority:

1. before Capsule admission;
2. again before body creation;
3. after body creation and before Hermes submission;
4. during restart recovery.

This closes the cancellation window around disposable-body creation.

If authority becomes inactive after the container exists but before Hermes
submission, Fleet performs no Hermes start and destroys/finalizes the body.

If an already-known Hermes run is recovered after its authority is
cancelled/revoked, Fleet calls the exact Hermes run stop operation, proves
finalization/quiescence, revokes temporary grants, destroys the container and
finalizes the Capsule.

A `run_submitting` state with unknown Hermes acceptance remains indeterminate;
authority cancellation does not manufacture certainty about an unknown
submission outcome.

## Host-action effect boundary

`HostActionBroker` now accepts an optional RunAuthority-state checker. The vNext
path can bind this directly to `RunAuthorityStore.effect_active`; when the store
is configured with a Phase 9 principal-state checker, the same effect gate also
fails closed for principal revocation/rebinding.

Authority is rechecked:

- when the broker request is admitted;
- again immediately before the host effect.

A cancelled, revoked or expired RunAuthority therefore cannot execute a new
brokered host effect even if an older bound grant object still exists in memory.

Legacy broker callers may omit the checker until Phase 37 migration removes the
old path.

## Authority remains separate from knowledge

RunAuthority can only be minted/validated by deterministic Fleet logic.

The following cannot widen it:

- prompt text;
- model output;
- memories;
- learned skills;
- Templar output;
- discovery/proposal output.

Learning may persist later under policy. Authority never persists into the Agent
Instance.

## Current local proof

Before PR/CI closure, current focused proof includes:

- canonical serialization/content hash/audit hash;
- HMAC exact-hash attestation;
- exact grant and Capsule derivation;
- closed nested-schema rejection;
- digest-pinned runtime image enforcement;
- exact deadline and not-yet-valid rejection;
- policy/capability/target/principal/Agent/Recipe staleness rejection;
- model/provider constraint enforcement;
- durable replay/idempotency/Capsule-claim fencing;
- concurrent exact admission and Capsule-claim convergence;
- durable pre-claim narrowing with immutable superseded-parent lineage;
- narrowing-after-Capsule-claim denial;
- cancellation/revocation persistence across store reopen;
- bound-principal revocation propagates through the authority effect-state check;
- symlinked store-parent denial and exact SQLite schema validation;
- monotonic narrowing negative tests;
- persisted authority tamper failure;
- authority-gated Capsule lifecycle;
- cancellation before Capsule admission;
- cancellation during body creation with body cleanup and no Hermes start;
- cancellation of a known running Hermes run with finalization and cleanup;
- host-action authority-state recheck immediately before effect;
- real Docker Capsule lifecycle under an actual RunAuthority.

Current local implementation proof before PR:

- combined RunAuthority/Capsule/broker/network/filesystem focused slice:
  **101 passed**;
- full Fleet Python suite on the verified Hermes environment:
  **973 passed, 1 skipped**;
- independent CPython 3.11.15 quality-style suite with the plugin-install smoke
  excluded: **971 passed, 2 skipped**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS.

These local results are not canonical closure evidence. Exact PR-head CI and the
resulting `main` push CI remain mandatory.

## Closure gates

Phase 10 is complete only after:

1. focused authority/replay/narrowing/revocation tests pass;
2. the authority-derived Run Capsule real-Docker lifecycle passes;
3. host-action effect-boundary revocation tests pass;
4. full Fleet Python tests pass;
5. independent Python 3.11 compatibility passes;
6. full Ruff, formatting, `git diff --check` and public hygiene pass;
7. exact PR-head CI is fully green, including Hermes clean-install smoke;
8. the PR merges normally without history rewriting;
9. resulting Fleet `main` push CI is fully green on the exact merge commit.

Phase 11 remains locked until every gate above closes.
