# vNext Phase 19: Deterministic security event model

Status: **COMPLETE — MERGED AND VERIFIED ON FLEET `main`**

Phase 19 defines the immutable, versioned Fleet security-fact model that later Templar phases consume. It deliberately does **not** implement Templar, create an authorization decision, activate a RunAuthority, or grant execution power.

The Phase 19 boundary is simple:

```text
authoritative Fleet request facts
        +
deterministic risk/interception/quarantine facts
        |
        v
fleet.security-event.v1

Fleet deterministic hard deny
        |
        v
fleet.security-hard-deny.v1   (separate object)
```

A security event is evidence. A deterministic hard deny is a Fleet policy result bound to that exact evidence. Neither object is execution authority.

## Ownership boundary

### Fleet owns

- canonical security-request projection and exact request hashing;
- immutable/versioned security-event facts;
- exact principal, Recipe/ResolvedRecipe, RunAuthority, target, requested-tool, authorized-toolset, resource, network, policy, and capability binding;
- bounded memory/skill risk facts;
- bounded secret-interception facts without intercepted values;
- deterministic policy-mismatch facts;
- Phase 16/17 quarantine/verification signals without learned-skill bodies;
- deterministic hard-deny records as a separate schema;
- exact event/hash validation before a later evaluator may consume the facts.

### Phase 19 does not give Templar ownership of

- Fleet authorization;
- RunAuthority issuance/activation;
- node operation;
- Docker, Keryx, Nodescale, Hermes, or host-action control;
- policy mutation;
- secret bodies;
- memory/skill bodies;
- learning promotion.

Templar itself remains Phase 20 work.

## Canonical schemas

Phase 19 introduces three closed schemas:

- `fleet.security-request.v1`;
- `fleet.security-event.v1`;
- `fleet.security-hard-deny.v1`.

Closed schema means unknown fields are rejected during deserialization. This matters at a security boundary: a caller cannot smuggle an unreviewed `authority`, `verdict`, `secret_body`, arbitrary prose instruction, or later-version field into a v1 record and have Fleet silently ignore it.

Every schema uses canonical JSON and SHA-256 content identity. Lists whose order has no semantic meaning are sorted before hashing. Duplicate facts fail closed instead of being silently collapsed.

## Exact request binding

`fleet.security-request.v1` binds the exact request facts that security judgments must refer to:

- exact `PrincipalReference` including principal generation and binding hash;
- Recipe hash;
- ResolvedRecipe hash;
- Recipe compiler version;
- requirement provenance digest;
- optional exact Workflow hash/step identity when the RunAuthority carries it;
- exact RunAuthority content hash;
- exact bounded RunAuthority target document plus its independently revalidated target digest;
- explicitly requested tool identities;
- exact RunAuthority-authorized toolsets;
- CPU, memory, PID, model-iteration, and exact RunAuthority deadline limits;
- exact network mode, network policy hash, destinations, ports, resolved public IPv4 addresses, and approval reference when applicable;
- policy digest;
- capability-set hash.

The request hash is the SHA-256 digest of that canonical request document. Any mutation of those request facts changes the request hash.

Phase 19 builds the event from an exact immutable `RunAuthority` object. The object need not already be registered as active durable authority merely to compute its content hash. This preserves the later Phase 22 ordering requirement: a proposed immutable authority document may be security-evaluated before Fleet admits/activates it as execution authority.

## Security event identity

`fleet.security-event.v1` contains:

- the exact request hash;
- the complete canonical request document;
- memory/skill risk signals;
- secret-interception facts;
- deterministic policy mismatches;
- quarantine signals.

The event has its own content hash in addition to the request hash.

This distinction is intentional:

- changing requested execution semantics changes both the request hash and event hash;
- changing only derived security evidence leaves the request hash stable but changes the event hash.

Later consumers therefore have both identities available and do not need to pretend that derived risk evidence was part of the user/request semantics.

## Memory and skill risk facts

A `MemorySkillRisk` carries only bounded metadata:

- subject kind: `memory` or `skill`;
- exact subject content/candidate hash;
- scope kind;
- deterministic risk level;
- canonical signal codes;
- evidence hash.

It does not contain the memory body, learned-skill source/body, prompt text, hidden instructions, or arbitrary evaluator prose.

The initial risk levels are:

- `info`;
- `low`;
- `medium`;
- `high`;
- `critical`.

Risk facts are descriptive. They do not grant or deny anything by themselves.

## Secret interception without secret bodies

A `SecretInterceptionFact` may contain only:

- source kind;
- detected classification kinds;
- count;
- action;
- deterministic sanitized evidence hash.

Accepted actions are:

- `none`;
- `redacted`;
- `blocked`;
- `vault-referenced`;
- `failed-closed`.

There is intentionally no field for:

- secret value;
- secret body;
- raw credential;
- password/token text;
- a direct hash of the intercepted secret value.

That last rule avoids turning low-entropy credentials into durable dictionary-attack targets. The event may bind a sanitized detector/evidence record hash, but not the secret itself.

Count/action consistency is validated: zero detections require `action=none`; positive detections require at least one classification and a non-`none` action.

## Deterministic policy mismatches

A `PolicyMismatch` is deliberately fact-only. It contains:

- mismatch code;
- bounded subject identifier;
- optional expected hash;
- optional observed hash;
- required evidence hash.

It has no `verdict`, `effect`, `allow`, `deny`, or authority field.

Expected/observed hashes are optional because not every deterministic mismatch is naturally represented as two content hashes. The required evidence hash still binds the exact deterministic evidence record without embedding unbounded or sensitive details.

## Quarantine signals

A `QuarantineSignal` carries the Phase 16/17 evidence needed for later evaluation without carrying candidate content:

- candidate hash;
- quarantine digest;
- state: `rejected`, `needs-review`, or `verification-ready`;
- reason digest;
- canonical reason codes;
- verification state: `not-run`, `verified`, or `failed`;
- Phase 17 verification digest when verification ran.

This preserves the existing rule that verification/quarantine evidence is not authority and does not activate the candidate.

## Network fact validation

Phase 19 does not invent a second network validator.

Serialized network destinations are revalidated through the existing Phase 4 `NetworkDestination` contract. The security-event network section also enforces the existing mode relationships:

- `none` and `provider-only` cannot carry direct destinations or an internet approval;
- `project-allowlist` requires exact destinations and no ad-hoc internet approval;
- `explicitly-approved-internet` requires exact destinations plus a separate approval reference.

The security event therefore cannot contain a network posture that Fleet's real network-authority model would reject.

## Hard denies are separate

Deterministic Fleet hard denies use `fleet.security-hard-deny.v1` and are never embedded inside `fleet.security-event.v1`.

One hard deny binds:

- request hash;
- event hash;
- policy digest;
- deterministic deny code;
- bounded subject identifier;
- evidence hash.

Validation rejects a hard deny if the request hash, event hash, or policy digest no longer matches the exact event. This prevents stale-deny reuse and request substitution.

Keeping hard denies separate preserves the Phase 22 gate ordering: Fleet can stop an already-hard-denied request without invoking Templar, while the underlying security event remains a neutral immutable fact record.

## No-authority invariant

Phase 19 objects cannot:

- grant a tool or toolset;
- grant filesystem access;
- grant network access;
- grant Vault/secret access;
- grant broker/host actions;
- alter approval budgets;
- widen resources;
- mutate Recipe/ResolvedRecipe;
- mutate or activate RunAuthority;
- activate or promote a learned skill;
- change principal identity/trust;
- invoke Templar;
- produce `ALLOW`, `DENY`, or `REVIEW` as a Templar verdict.

The security-event module contains no executor, transport client, Docker client, Keryx client, Nodescale client, Vault secret reader, or Hermes tool invocation path.

## Determinism and fail-closed behavior

Phase 19 proves:

- frozen dataclasses for all event facts;
- canonical JSON serialization;
- exact content hashes;
- canonical ordering for set-like tuples;
- duplicate rejection;
- closed-version deserialization;
- exact request-hash revalidation on deserialize;
- exact hard-deny event/hash/policy revalidation;
- authoritative Phase 4 network destination validation;
- bounded counts and list sizes;
- malformed/unknown schema rejection;
- missing deterministic target identity rejection;
- no intercepted secret-body field.

## Local acceptance evidence

Phase 19 implementation and local acceptance evidence:

- focused Phase 19 suite: 12 passed;
- exact pinned Hermes Agent integration environment uses revision `16589473f7fe47fdec72b69cdc6f1039228744b9`, matching Fleet CI;
- full Fleet suite in that clean Python 3.11 environment: 1004 passed, 12 environment-only skips, including the clean Hermes plugin install/registration smoke;
- full Ruff lint across `hermes_fleet`, dashboard, scripts, and tests: green;
- public-hygiene scan: green;
- Desktop plugin JavaScript syntax check: green;
- Python package sdist/wheel build: green;
- `git diff --check`: green;
- Rust `cargo fmt --all -- --check`: green;
- Rust `cargo clippy --workspace --all-targets -- -D warnings`: green;
- Rust `cargo test --workspace`: green;
- Rust `cargo build --workspace`: green;
- event round-trip/hash stability: green;
- request-substitution and stale-hard-deny tests: green;
- exact target-document re-hash rejects forged target summaries even when an outer request hash is recomputed: green;
- exact RunAuthority deadline binding: green;
- explicit quarantine verification-state/digest consistency: green;
- secret-body closed-schema test: green;
- malformed target/network/interception/quarantine tests: green.

Fleet delivery evidence is final:

- implementation PR #158 final exact head: `721c46cf3ac5047ba43e10cd25f8fe938692f292`;
- exact final-head PR CI run `32356964144`: completed successfully with Python 3.11/3.13 quality, Rust workspace compatibility, real Nodescale/readiness proofs, and the pinned-Hermes clean-install smoke all green;
- PR #158 merged normally as `231e55f8ee202993135776ab76ae976e8c7c7380`;
- exact post-merge `main` CI run `32357490741`: completed successfully on that merge SHA with all five jobs green, including the pinned-Hermes clean-install smoke;
- the two earlier PR runs failed only repository quality formatting/lint gates on superseded heads and were repaired with forward commits; no failed behavioral/security test was bypassed, no history was rewritten, and no force push or administrative merge override was used.

Phase 19 therefore satisfies the repository closure rule end to end. The canonical next implementation entry point is Phase 20.

## Later-phase boundary

Phase 19 does **not** implement:

- Phase 20 Templar core/verdict model;
- Phase 21 disposable Templar sandbox;
- Phase 22 pre-execution Templar gate;
- Phase 23 Templar learning/promotion gate;
- Phase 26 full audit/provenance chain.

Phase 20 may consume these immutable facts, but it may not reinterpret them as authority. Fleet deterministic hard deny remains authoritative and later Templar `ALLOW` can never grant execution by itself.
