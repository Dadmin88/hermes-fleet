# Hermes Fleet vNext Phase 6 acceptance: persistent Hermes Agent Instances

Status: **COMPLETE**

Phase 6 introduces the durable brain half of the vNext architecture. A Fleet Agent Instance is a persistent Hermes-native profile that survives jobs and disposable execution bodies. It is not a Run Capsule, does not own temporary authority, and is never deleted as part of normal run cleanup.

This phase creates the new persistent-Agent path alongside the existing legacy `fleet-execution` runtime. The old disposable-profile execution path remains rollback-compatible until Phase 7/8 provide the complete run-scoped/container execution path and Phase 37 performs the final migration/removal. Phase 6 does not pretend the legacy path has already disappeared.

## Stable Agent identity

Persistent identity is derived from the stable Agency profile identity:

- Agency repository;
- Agency profile name.

The stable Agent Instance ID deliberately excludes:

- pinned Agency revision;
- Agency version;
- Agency content digest;
- job/run ID;
- container ID;
- temporary principal/run authority.

The canonical stable identity is content-addressed and maps to a deterministic native Hermes profile name of the form `fleet-agent-…`.

Excluding the pinned base version is essential: a later explicit Agency base upgrade must preserve the same durable brain. Phase 24 owns that base+overlay upgrade procedure.

Principal identity is not baked into the Agent Instance ID in Phase 6. Principal authentication and memory/skill scope are formalized in Phases 9 and 11. This avoids creating an early parallel principal model while preserving the plan’s later ability to store principal-scoped state inside one persistent Agent brain.

## Hermes native profile substrate

`AgentInstanceManager` materializes the exact immutable Agency bundle into the normal Hermes profiles root. It does not invent a second profile or memory format.

Initial creation:

1. resolves the stable Agent identity;
2. securely loads the destination model/provider baseline;
3. materializes the exact verified Agency bundle into a private staging directory;
4. rejects reserved Fleet run/credential state supplied by the Agency bundle;
5. merges only the durable model/provider baseline into Hermes `config.yaml`;
6. verifies the persistent config contains no run-scoped fields;
7. writes bounded Fleet Agent metadata/state/lock files;
8. atomically publishes the staged native profile.

Concurrent creators do not merge mutable state. One atomic rename wins; the other creator discards its staging copy and validates the winner against the exact requested base and model baseline.

## Durable Agent metadata

`.fleet-agent-instance.json` contains only durable identity/base information:

- stable Agent Instance ID;
- native Hermes profile name;
- Agency repository/name;
- exact currently installed Agency base revision/version/content digest;
- model-baseline digest;
- persistent profile-config digest.

It contains no run power.

`.fleet-agent-state.json` contains only generation counters for durable mutation coordination:

- memory generation;
- skills generation.

`.fleet-agent-state.lock` is a local locking primitive only.

Fleet does not store memory rows or skill content in these files. Actual memory/skill persistence remains Hermes-native; the generations only serialize/version future durable mutations.

## No run state in persistent profile

A persistent Agent Instance may contain the Agency base, Hermes profile metadata/config, native memory state, approved learned skills, and future durable overlays.

It may not contain temporary run state.

Phase 6 explicitly rejects legacy/temporary files such as:

- `.env` credentials;
- `.fleet-execution-owner`;
- `.fleet-execution-slot`;
- RunAuthority/Run Capsule marker files.

Persistent `config.yaml` is recursively checked for run-scoped keys including container binding, `fleet_runtime`, approval budgets, RunAuthority/RunCapsule, secret references/handles, network/filesystem grants, and host-broker grants.

The durable config bytes are hashed at creation and revalidated on every reopen. A changed baseline/config fails closed with `AgentInstanceConfigurationChanged`; it is never silently rewritten by a run.

## Exact-base reuse and upgrade-required behavior

For the same stable Agency identity:

- identical exact Agency base → reuse the existing Agent Instance;
- changed base revision/version/content → keep the same stable Agent ID but fail closed with `AgentInstanceUpgradeRequired`;
- changed model/provider baseline → fail closed with explicit-update-required behavior.

This deliberately avoids silently erasing or reconciling durable learning. Phase 24 owns exact base+overlay upgrade mechanics.

## Mutable brain versus immutable Agency base

Once an Agent learns a skill, its mutable Hermes profile content no longer equals the immutable Agency package bytes. Fleet therefore cannot use the live mutable profile digest as proof of which immutable Agency base is installed.

`profile_inventory` now detects Fleet-managed persistent Agent metadata and advertises the exact immutable base name/version/content digest recorded in the validated Agent Instance metadata. Generic Hermes profiles retain the previous live content-digest behavior.

The inventory path validates persistent Agent metadata as a private current-user-owned, single-link regular file before trusting it. Invalid Fleet Agent metadata fails closed instead of falling back to mutable profile inference.

A regression adds a learned skill, proves the live mutable profile digest changes, and still verifies that Fleet advertises the original exact Agency base identity.

## Durable memory/skill concurrency seam

`mutation_guard()` provides a small coordination primitive around future Hermes-native memory/skill writes.

For each component (`memory` or `skills`) it:

- acquires a process-local reentrant lock;
- acquires a POSIX advisory file lock for cross-manager/process coordination;
- reads the current component generation;
- requires the caller’s expected generation to match;
- yields the mutation window to the caller;
- advances only that component generation after successful completion.

If the native mutation raises, the generation does not advance.

Concurrent callers starting from the same generation serialize; once the first commits, the second fails with `AgentInstanceConflict` instead of blindly overwriting newer durable state.

The guard does not attempt to implement Hermes memory or skill storage itself. Later memory/skill phases wrap Hermes native mutation APIs with this or an evolved equivalent.

## On-disk trust boundary

Persistent Fleet control files and `config.yaml` must be:

- regular files;
- non-symlinks;
- owned by the current service user;
- mode `0600`;
- single-link files;
- within explicit byte bounds.

The lock file is checked both by path metadata and again after `O_NOFOLLOW` open before locking.

Symlinked/oversized/incorrect Agent metadata, state, lock, or config fails closed.

## Persistence/restart proof

The focused persistence suite creates an Agent Instance, writes a learned skill into the Hermes-native profile, advances its skill generation, then starts a completely fresh Python process.

That new process:

- constructs a new `AgentInstanceManager` from only the profiles root/model baseline;
- derives/reopens the same stable Agent Instance;
- sees the same profile/instance ID;
- sees the persisted skill generation;
- reads the learned skill created before the process boundary.

No in-memory Fleet manager state is required. The durable brain is ordinary on-disk Hermes profile state, so container destruction and Fleet/Hermes process restart do not erase it. Full machine-restart and positive-learning end-to-end proofs are repeated later in the dedicated persistence/fault/release phases.

## Concurrent creation and run-config collision proof

The test suite proves:

- two independent managers racing to create the same stable Agent produce one persistent profile;
- no `.creating-*` staging residue remains;
- memory/skill mutations do not change persistent `config.yaml` bytes;
- a run-scoped config insertion causes reopen to fail closed;
- exact-base reuse preserves learned skill content;
- the manager intentionally exposes no per-run `cleanup()` deletion primitive.

## Current proof

Phase 6 current proof:

- focused Agent Instance + profile-inventory + fresh-process persistence suite: **28 passed**;
- full Fleet Python suite: **827 passed, 1 skipped**;
- full Ruff: PASS;
- `git diff --check`: PASS;
- public-hygiene scan: PASS.

The single skipped Fleet test is an existing environment-conditional integration check and is not a Phase 6 Agent Instance failure.

No Hermes Agent repository change is required for the durable storage model itself. Phase 6 intentionally builds on Hermes’ existing native profile directory/config/memory/skill substrate. Phase 7 is the Hermes change that adds the narrow run-scoped temporary execution override so runs can use this persistent brain without modifying its durable config.

## Explicit non-goals retained for later phases

Phase 6 does not yet:

- send a Fleet container binding through Hermes `/v1/runs`: Phase 7;
- orchestrate complete Run Capsule lifecycle/recovery: Phase 8;
- establish formal principal identity: Phase 9;
- issue/verify complete immutable RunAuthority: Phase 10;
- implement principal-scoped memory retrieval: Phase 11;
- implement full base+learned-overlay upgrades: Phase 24;
- remove the old disposable-profile execution path: Phase 37.

The durable Agent Instance contract is now ready for the run-scoped execution seam without mixing brain state and temporary body/authority state.
