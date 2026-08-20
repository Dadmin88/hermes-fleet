# vNext Phase 18: Memory/skill promotion

Status: **IN PROGRESS — IMPLEMENTED LOCALLY; FINAL MERGE/CI CLOSURE PENDING**

Phase 18 implements explicit durable-learning promotion. It widens the visibility scope of exact approved memory or learned-skill content without creating execution authority. Promotion remains separate from Phase 17 verification: `verified` is evidence, `promoted` is an explicit scoped visibility transition, and neither state grants tools, filesystem/network access, secret bodies, broker power, approvals, or RunAuthority mutation.

## Ownership boundary

### Fleet owns

- deciding whether one exact promotion is authorized;
- validating the exact authenticated administrator for the target scope;
- requiring a private-source promotion administrator to be the exact scoped child identity derived from that private source principal, or the owner principal itself for owner scope;
- monotonic scope widening only: `principal -> project -> network -> owner`;
- binding source owner, Agent Instance, source scope, target scope, source content hash, final approved content hash, administrator generation/binding, verification evidence, operation, current-version expectation, and expiry into a canonical short-lived promotion authorization;
- optimistic conflict policy through `expected_current_promotion_id`;
- exact rollback authorization;
- requiring Hermes to advertise `fleet_learning_promotion` before the promotion API is used.

### Hermes Agent owns

- native Fleet-scoped memory persistence;
- native learned-skill candidate bundles and their Phase 16/17 quarantine/verification evidence;
- re-reading the exact durable source before commit;
- sanitizing memory and textual skill content before Fleet approves the final hash;
- failing closed when skill content is opaque binary material that cannot be proven sanitized;
- re-running the Phase 17 skill verification boundary before skill promotion;
- requiring a real current promoted source version for non-private skill widening such as `project -> network`;
- materializing immutable promoted skill versions outside the normal active skill tree;
- persisting private promotion state/history and enforcing exact current-version conflicts;
- append-only rollback materialization;
- exposing promoted skills only to runs whose Fleet read scopes include the promoted scope;
- checking promoted bundle integrity again at retrieval time;
- keeping all promoted learning `authority: none`.

## Promotion authorization

Fleet emits `fleet-promotion-v1` under policy `phase18-v1`.

One authorization binds:

- subject kind: `memory` or `skill`;
- stable subject key;
- source owner principal ID;
- Agent Instance ID;
- exact source scope;
- exact broader target scope;
- source content hash;
- final approved/sanitized content hash;
- exact administrator principal ID/kind/generation/binding hash;
- issue and expiry timestamps;
- Phase 17 verification digest for skills;
- exact expected current promotion ID when replacing/rolling back;
- exact rollback target when applicable;
- operation: `promote` or `rollback`;
- `authority: none`.

The promotion ID is the SHA-256 digest of the canonical authorization document. Any mutation of the request changes the ID and fails independent Agent validation.

Authorizations are short-lived and cannot narrow/equal-scope masquerade as promotion. Source and target scope ranks must increase monotonically.

## Scope-administrator policy

A promotion may be authorized only by the exact administrator of the target scope.

For a private `principal` source, Fleet additionally requires consent lineage:

- project/network administrators must be scoped child principals derived from the exact current source principal reference;
- owner-scope promotion uses the owner principal itself;
- an unrelated administrator with the same project/network label is insufficient.

For a source that is already shared, the exact broader target-scope administrator controls the next widening hop. Hermes independently proves that a non-private skill source really exists as one exact current promoted version before allowing the next hop.

## Prepare before approve

Promotion is two-stage.

1. Hermes prepares the exact promotable material without mutating promoted state.
2. Fleet authorizes the exact final hash returned by preparation.
3. Hermes re-reads/re-verifies the source and commits only if the prepared values still match the short-lived authorization.

This prevents Fleet from approving the private source hash and then discovering that sanitization changed the content after approval.

### Memory preparation

Hermes:

- reads one exact Fleet memory entry from the declared source scope;
- verifies source owner and Agent Instance metadata;
- rejects revoked or content/metadata-drifted sources;
- applies the existing always-on secret/private-data redaction boundary;
- returns the exact hash of the sanitized content.

The original private/shared source remains unchanged. The promoted target scope receives only the sanitized copy.

### Skill preparation

Hermes:

- locates the exact immutable Phase 15 candidate;
- reconstructs its exact original Fleet learning binding;
- requires a valid Phase 16 quarantine seal;
- re-runs Phase 17 exact-hash verification;
- confirms the candidate remains inactive and `authority: none`;
- sanitizes every textual bundle file;
- rejects opaque binary files because secret/private-data sanitization cannot be proven;
- computes the exact sanitized bundle manifest hash;
- returns the Phase 17 verification digest with the final approved bundle hash.

Any candidate, quarantine, verification, capability-manifest, or sanitized-content change invalidates promotion.

## Durable promotion state

Hermes stores Phase 18 state under a private profile-local promotion root.

The store:

- safely initializes a fresh Hermes home when its existing parent is a real directory;
- rejects symlinked/non-directory Hermes-home or promotion-state components;
- uses private POSIX permissions for newly created promotion state;
- rejects unsafe multi-link/symlink state files;
- serializes mutations under a cross-platform process lock;
- validates the opened lock file itself rather than trusting only its pathname;
- uses atomic JSON replacement for durable state/records.

Promotion subject identity binds:

- subject kind/key;
- source owner principal;
- Agent Instance;
- source scope;
- target scope.

This prevents identical content from different principals, Agent Instances, or promotion paths from sharing version history accidentally.

## Versioning, conflicts, and rollback

Every successful promotion creates an immutable promotion record and advances one exact subject-state pointer.

Rules:

- first promotion expects no current version;
- later replacement requires the exact current promotion ID;
- stale current-version expectations fail closed;
- exact replay of an already-current promotion is idempotent;
- rollback never rewrites history;
- rollback creates a new promotion event that references one exact historical target;
- skill rollback materializes a fresh exact bundle for the rollback event instead of pointing at an older mutable path;
- memory rollback revokes the former current promoted entry and re-activates the exact historical approved hash;
- history responses contain hashes/IDs/evidence only, not secret bodies or raw private content.

## Multi-hop promotion

Memory promotion proves the shared source directly because preparation must read the exact source-scope memory row.

Skill promotion has an additional source-proof rule. When `source_scope` is not `principal`, Hermes requires exactly one current promoted state for the same candidate/source owner/Agent Instance whose target equals the requested source scope. The source promoted bundle must still match the candidate's current sanitized hash and Phase 17 verification digest.

Therefore a caller cannot claim `source_scope=project` to bypass the private-owner gate and jump directly to `network`.

When the same immutable candidate is visible through several authorized scopes, Hermes selects its broadest exact current promoted version. Different candidate identities that produce the same visible skill name remain an ambiguity and fail closed.

## Retrieval and context safety

Promoted memory uses the existing Phase 11/12 shared-memory rules:

- non-principal memory is visible only when `promotion_state=promoted` and `trust=promoted`;
- the current Fleet run must include the scope in its authorized read scopes;
- the context firewall revalidates identity, retention, revocation, sensitivity, trust, content hash, stored-instruction threats, secret material, and authority-manipulation text before prompt construction.

Promoted skills remain outside Hermes's normal active skill tree. Phase 18 adds them to discovery only when the current run's Fleet read scopes include their target scope. Retrieval re-hashes the materialized bundle and fails closed if it changed after approval.

A promoted learned skill cannot silently shadow a native/Agency active skill. A collision is an explicit conflict until Phase 24 implements base-plus-overlay reconciliation.

## No-authority invariant

Promotion does not and cannot:

- grant a tool or toolset;
- grant filesystem access;
- grant network access;
- grant secret/Vault access;
- grant broker/host actions;
- change approval budgets;
- alter resource limits;
- mutate Recipe/ResolvedRecipe;
- mutate RunAuthority;
- activate the original quarantined candidate in the normal native skill tree;
- turn persisted text into policy.

Every Agent promotion response and durable promotion record carries `authority: none`. Fleet rejects promotion API responses that attempt to carry any other authority value.

## API/mixed-version boundary

Hermes advertises `fleet_learning_promotion` only when the Phase 18 API exists.

Authenticated endpoints:

- `POST /v1/fleet/promotions/prepare`
- `POST /v1/fleet/promotions/commit`
- `POST /v1/fleet/promotions/rollback`
- `POST /v1/fleet/promotions/history`

Fleet's Hermes client requires the capability before using those endpoints and validates the response object shape plus `authority: none`.

## Local acceptance evidence

### Hermes Agent

Current Phase 18 local evidence on the final Agent implementation:

- Phase 11-18 Agent/Gateway regression chain: 136 passed after multi-hop hardening;
- focused skill-promotion suite: 8 passed;
- clean-home and symlink-root regressions: green;
- full Ruff: green;
- `ty` on the Phase 18 promotion modules: green;
- `git diff --check`: green;
- repeated CI-flake regression for the same-name candidate fixture: 12 consecutive passes;
- Fleet -> Agent cross-repo memory-promotion proof: `PHASE18_AGENT_PROMOTION_SEAM_OK`.

Agent delivery evidence is final:

- implementation PR #15 exact final head `5e00116542f22df743c3cf85104dbceeb9b0171c` completed CI run `32344925755` successfully and merged as `16313b124fbf3087e4b4a35cd5c34e3f22f44adc`;
- follow-up hardening PR #16 exact final head `ba330450c68c2a215370815de0f4773996c6793a` completed CI run `32346356717` successfully and merged as final Phase 18 Agent revision `16589473f7fe47fdec72b69cdc6f1039228744b9`;
- Docker post-merge run `32346944264` completed successfully on that final merge SHA;
- automatic CI push dispatch `32346944272` on that SHA terminated before creating any jobs and could not be rerun; it produced no failing job/check evidence;
- canonical CI was then dispatched on `main` at the exact same SHA as run `32347113842` and completed successfully with 48 jobs and zero failures.

### Fleet

Current Phase 18 Fleet evidence:

- promotion/Runs focused suite: 40 passed;
- full clean local suite with final pinned Agent and Fleet installed in the same environment: 991 passed, 13 environment-only skips, including the clean plugin-install acceptance;
- full Fleet Ruff: green;
- public-hygiene scan: green;
- Desktop plugin syntax: green;
- `git diff --check`: green;
- Fleet CI now pins final Agent revision `16589473f7fe47fdec72b69cdc6f1039228744b9`;
- the exact new CI heredoc locally executes a real `principal -> project -> network` memory promotion and prints `PHASE18_AGENT_PROMOTION_SEAM_OK`.

Fleet PR-head CI, Fleet post-merge `main` CI, and final progress-ledger closure remain pending. Phase 18 must not be marked complete until those gates are green.

## Later-phase boundary

Phase 18 does **not** implement:

- Phase 19 deterministic security-event modeling;
- Phase 20-23 Templar evaluation/gates;
- Phase 24 Agency-base upgrade and learned-overlay reconciliation;
- Phase 25 right-to-forget derived-state deletion;
- Phase 35 operator promotion UI.

Phase 18 supplies the deterministic, auditable promotion primitive those later phases can evaluate and operate without granting authority themselves.
