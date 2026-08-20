# vNext Phase 14: Scoped Vault references

Status: **COMPLETE**

Phase 14 implements the master-plan boundary for scoped secret custody and runtime-only retrieval. It extends the existing vNext `secret_refs` / RunAuthority / Run Capsule contracts rather than placing secret bodies in Fleet-owned durable state.

Phase 14 is complete only when all three implementation layers have merged with green PR CI and the resulting required `main` validation gates are green:

1. the dedicated Vault custody component;
2. Hermes Agent runtime redemption/injection;
3. Hermes Fleet authorization, temporary-handle lifecycle, and exact dependency seams.

## Ownership boundary

### Vault owns

- secret bodies;
- encrypted-at-rest custody;
- ownership and scope metadata;
- immutable versions;
- rotation;
- expiry;
- revocation;
- opaque stable references;
- temporary per-run handles;
- value-free access audit.

### Fleet owns

- durable symbolic `secret_refs` in Recipe/RunAuthority/Run Capsule authority;
- exact principal and RunAuthority authorization before a reference is usable;
- deriving principal/project/network/owner scope from current identity and exact run authority;
- minting temporary run handles through Vault;
- passing only opaque handles plus safe injection metadata to Hermes;
- revoking all handles for an execution after exact-run quiescence or on definite no-run cleanup;
- retaining handles for an indeterminate possibly-live run until quiescence, explicit revocation, or expiry rather than revoking underneath possible work.

Fleet does **not** receive secret bodies.

### Hermes Agent owns

- validating the `fleet-vault-v1` run binding before run creation;
- preserving ContextVar isolation between concurrent runs;
- redeeming temporary handles only inside trusted runtime paths;
- injecting environment values at the actual Fleet-owned command spawn;
- injecting file values into the disposable container's `/tmp` tmpfs;
- exposing an internal broker redemption helper for trusted host/runtime consumers;
- keeping protected values out of model-visible tools, Agent Instance state, session snapshots, command argv, and API run status.

Hermes does **not** expose a model tool that returns a protected value.

## Scope authorization

The Vault principal context is derived from the exact current Fleet `PrincipalRecord` plus the exact `RunCapsuleSpec`:

- principal scope is always the exact principal ID;
- project scope is admitted only from the exact RunAuthority-backed Capsule `project_scope`;
- network scope comes from current principal membership;
- owner scope comes from current principal ownership metadata.

Durable principal membership alone cannot widen project access for a run. Cross-principal and out-of-scope reference use fails closed.

## Stable references and temporary handles

Durable Fleet authority contains only stable opaque references. Before Hermes submission, Fleet asks Vault to authorize those exact references for the exact run identity and deadline. Vault returns temporary opaque handles bound to:

- exact run ID;
- exact principal identity/generation/binding;
- exact RunAuthority hash;
- exact material version;
- exact injection kind/target;
- bounded expiry;
- bounded use count.

Temporary handles are not written into Run Capsule durable state. Vault indexes handles by run ID, so Fleet can revoke all handles after restart without persisting the handle strings itself.

## Runtime injection paths

### Environment

The protected value is redeemed only for a real Fleet terminal command. It crosses from Hermes to `docker exec` over stdin and is exported inside the child shell. It is not placed in Docker argv, the Agent process environment, or the reusable shell snapshot.

### File

The protected bytes cross only over `docker exec -i` stdin and are written under `/tmp/hermes-secrets` in the disposable container with private directory/file modes. The file path is injection metadata; the body is not present in command arguments.

### Broker

Hermes exposes an internal Python redemption helper keyed by the pre-authorized broker target. It is intentionally not registered as a model tool. The existing profile credential broker file is protected by the local Katana owner-mode path policy, so Phase 14 does not bypass that machine safety boundary to modify it.

## Durable-state invariants

- no protected body in Agent Instance state;
- no protected body in Run Capsule state;
- no temporary handle in Run Capsule state;
- no protected body in model-visible run status;
- no protected body in command argv;
- no protected body in Vault audit rows;
- stable opaque references may persist where explicitly authorized;
- Vault ciphertext, not plaintext, is the durable protected-body representation.

## Lifecycle and failure rules

- version rotation creates a new immutable version;
- old versions can be revoked independently;
- stable references resolve to the current active version when a run handle is minted;
- temporary handle expiry is no later than the run deadline and may be narrowed by material-version expiry;
- revoking an item/version/run invalidates subsequent redemption;
- definite pre-run failure revokes temporary handles before body cleanup;
- successful/failed/timed-out runs revoke after quiescence and before disposable-body cleanup;
- indeterminate possibly-live runs do not have their handles revoked underneath them; the handles remain bounded by expiry.

## Acceptance evidence

### Dedicated custody component

Repository: `Dadmin88/hermes-vault`

- PR #1 head: `fabc72ea93ca06b1cfb170f224145cdf80eb90b2`;
- merge commit: `bb047f19d08585d380bfcf0f2224300939b7ba90`;
- PR Python 3.11 and 3.13 CI: green;
- exact post-merge `main` CI run `32222105106`: green;
- local tests: 11 passed;
- Ruff, formatting, package build, and clean-wheel smoke: green.

### Hermes Agent

Repository: `Dadmin88/hermes-agent`

Implementation PR #9:

- head: `6d7eaacbd8660908b046999063e1278faa5a9b14`;
- merge commit: `217a0f552063a59c4f1da523af9512d73f21d89e`;
- full PR matrix: green after rerunning an HTTP-429-only history-check infrastructure failure on the same head;
- focused Phase 14 / Phase 12 / Phase 13 compatibility tests: 97 passed, 2 skipped;
- Fleet-workshop runtime subset: 53 passed;
- full Ruff and 966-file Windows portability scan: green;
- new runtime-material module: zero `ty` diagnostics;
- modified legacy files: no new `ty` diagnostics over Phase 13 baseline;
- exact Vault dependency pin: `bb047f19d08585d380bfcf0f2224300939b7ba90`.

Closure/recovery PR #10:

- reason: GitHub emitted no Actions run at all for implementation merge SHA `217a0f552063a59c4f1da523af9512d73f21d89e`, leaving no supported post-merge recovery path;
- change: one CI line adding `workflow_dispatch`, without changing jobs, permissions, or required-check semantics;
- head: `6bdaa161fc2fca200e81230a6e94ad8b7733f0bc`;
- PR CI attempt 2: green after the repository-required `ci-reviewed` label caused the failed review-label job to rerun on the same head;
- merge commit: `9cacb78c9984b9629db80c69f54b0d30c4017958`;
- delayed automatic push run `32232718588`: `startup_failure` before any jobs were created;
- manual full `main` validation run `32232988117`, attempt 2: `success` on exact SHA `9cacb78c9984b9629db80c69f54b0d30c4017958` with zero failed jobs; attempt 1 had one unrelated `test_turn_lease` timeout and its aggregate gate fail, then the failed slice passed unchanged on rerun.

The manual dispatch used the unchanged full CI orchestrator on the exact post-merge `main` SHA. It exists solely because the automatic post-merge event path failed at GitHub's Actions control plane before useful validation could complete.

### Hermes Fleet

Local pre-PR evidence:

- targeted Phase 14 / Runs / Run Capsule tests: 57 passed, including post-mint RunAuthority revocation and cross-principal denial;
- broader Fleet suite: 988 passed, 2 skipped;
- full Ruff and formatting: green;
- public-hygiene scan: green;
- Desktop plugin syntax: green;
- package build: green;
- clean-wheel entry-point and Phase 14 import smoke: green.

Fleet closure evidence is final:

- Fleet PR #151 exact head: `8b32b0dbcfb0c44a88d09f73a1344318d2085728`;
- exact PR-head CI run `32237210793`: completed successfully;
- merge commit: `31c8a62e6a1cd56decea4e6214c3ee24bf3315f7`;
- exact post-merge `main` CI run `32237499703`: completed successfully on the merge SHA.

The Vault, Agent, and Fleet layers have therefore all satisfied the Phase 14 closure rule.

## Later-phase boundary

This phase does **not** implement:

- Phase 15 scoped skill learning;
- Phase 16 skill quarantine;
- Phase 17 skill verification;
- Phase 18 memory/skill promotion.

No secret reference, handle, memory, skill, model output, or runtime injection can create or widen RunAuthority.
