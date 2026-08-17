# Hermes Fleet vNext Phase 6 acceptance: persistent Hermes Agent Instances

Status: **COMPLETE**

Phase 6 establishes the durable-brain half of the vNext architecture. A Fleet Agent Instance is a persistent Hermes-native profile that survives jobs, disposable Run Capsules/bodies, container destruction, Fleet/Hermes process restart, and machine restart. It is not temporary run state and is never deleted as normal run cleanup.

This phase deliberately leaves the legacy `fleet-execution` compatibility path in place. Phase 7 provides the run-scoped Hermes execution override and later migration phases remove the old disposable-profile path only after the replacement path has passed its own acceptance proofs.

## Stable Agent identity

Persistent identity is derived only from the stable Agency profile identity:

- Agency repository;
- Agency profile name.

The stable Agent Instance ID deliberately excludes:

- pinned Agency revision;
- Agency version;
- Agency content digest;
- job/run ID;
- container ID;
- temporary principal/RunAuthority state.

The identity maps deterministically to a native Hermes profile named `fleet-agent-…`. A changed immutable Agency revision/version/content therefore retains the same durable brain identity but fails closed with `AgentInstanceUpgradeRequired` until the explicit base+overlay upgrade phase performs the migration.

Principal identity is not invented early or embedded in the Phase 6 Agent ID. Formal principal identity and scoped memory/skill authority remain later phases.

## Hermes-native durable substrate

`AgentInstanceManager` uses the ordinary Hermes profiles directory as the durable substrate. It does not invent a second Agent storage format.

Initial creation:

1. derives the stable Agent identity;
2. securely loads the durable model/provider baseline;
3. materializes the exact verified immutable Agency bundle into a private staging profile;
4. rejects reserved Fleet run/credential/control state anywhere in the staged tree;
5. records a manifest of every immutable Agency-installed base file;
6. merges only the durable model/provider baseline into native `config.yaml`;
7. rejects run-scoped keys recursively from persistent config;
8. writes bounded private Agent metadata/state/base-manifest/lock files;
9. fsyncs staged files and directories;
10. atomically publishes the native Hermes profile and fsyncs the parent profiles directory.

Concurrent creators do not merge mutable profile state. One atomic publish wins; the other creator discards its staging directory and validates the published winner against the exact requested base and model baseline.

## Durable metadata is not run authority

`.fleet-agent-instance.json` stores only durable identity/base information:

- stable Agent Instance ID;
- native Hermes profile name;
- Agency repository/name;
- installed base revision/version/content digest;
- immutable base-manifest digest;
- model-baseline digest;
- persistent profile-config digest.

`.fleet-agent-base-manifest.json` stores the exact path, digest, size, and mode of every immutable Agency file that existed before Fleet merged the durable Hermes config baseline. Its canonical digest is itself bound into `.fleet-agent-instance.json`, so rewriting the manifest cannot silently redefine the installed immutable base.

`.fleet-agent-state.json` stores only durable mutation generations:

- memory generation;
- skills generation.

`.fleet-agent-state.lock` is only a local/process synchronization primitive.

Fleet does not store Hermes memory rows or learned-skill bodies in these control files. Memory and skills remain native profile content.

## Immutable Agency base versus mutable learned overlay

The immutable Agency base and mutable durable learning are separate trust domains.

At creation Fleet records every Agency-installed base file except `config.yaml`, which is separately protected by the persistent profile-config digest. On every reopen Fleet verifies each recorded base file by:

- walking from the validated profile directory with descriptor-relative `openat` semantics;
- applying `O_NOFOLLOW` to every directory and final file;
- requiring current-user ownership and a regular single-link file;
- bounding per-file and total bytes;
- comparing exact size, mode, and SHA-256 digest;
- rejecting files that change while being read.

Replacing an intermediate Agency directory with a symlink cannot redirect verification elsewhere.

New Hermes-native learned files created after Agent creation are not retroactively treated as immutable Agency files and therefore survive normal reuse. A regression adds a learned skill, verifies reuse, then modifies `SOUL.md`; both Agent reopen and profile inventory fail closed on the base drift.

`profile_inventory` advertises the immutable Agency name/version/content digest only after validating the persistent Agent metadata and immutable-base manifest. It does not infer the base from the live mutable profile digest after learning has changed the profile.

## No temporary run state in persistent profile

The durable Agent profile may contain the Agency base, normal Hermes profile config/state, approved durable memory/skills, and future durable scoped overlays. It may not contain temporary execution authority.

Phase 6 recursively rejects reserved files including Fleet execution ownership/slot markers, `.env`, RunAuthority/Run Capsule markers, Fleet runtime markers, and approval-budget markers.

Persistent `config.yaml` is recursively checked for normalized aliases of run-scoped state including:

- run/execution IDs and state;
- idempotency keys/digests;
- plan fingerprints;
- deadlines and resource limits;
- container IDs;
- Fleet runtime;
- approval budgets;
- RunAuthority/Run Capsule;
- temporary credential and secret references/handles;
- network grants;
- filesystem grants;
- host-broker grants.

The durable config bytes are hashed at creation and checked on every reopen. Drift fails with `AgentInstanceConfigurationChanged`; a run never silently rewrites the persistent baseline.

## On-disk trust boundary

The Agent profile directory must be a current-user-owned mode-`0700` directory. The Hermes profiles root must be current-user-owned, non-symlinked, and not world-writable. Fleet does not rewrite the existing Hermes profiles-root mode merely to manufacture a stricter layout.

Persistent Fleet control files and `config.yaml` must be current-user-owned, mode `0600`, regular, single-link files within explicit byte bounds.

Security-sensitive reads use `O_NOFOLLOW` descriptors and `fstat`, not pathname `lstat` followed by a separate pathname read. Reads are bounded and reject inode/size/timestamp changes during the read. Lock files are likewise opened and validated by descriptor.

Creation and metadata updates fsync the file and containing directory before the durable operation is considered complete.

## Exact-base reuse and upgrade-required behavior

For one stable Agency identity:

- identical exact Agency base + model baseline → reopen/reuse the existing Agent Instance;
- changed base revision/version/content → retain stable Agent identity but fail `AgentInstanceUpgradeRequired`;
- changed model/provider baseline → fail `AgentInstanceConfigurationChanged`;
- changed persistent config outside an explicit durable update → fail closed.

Learned profile content is preserved on exact-base reuse.

## Durable memory/skill concurrency seam

`mutation_guard()` coordinates future Hermes-native durable memory/skill mutation without implementing a parallel memory or skill store.

For `memory` or `skills`, the guard:

- validates the exact persistent Agent binding;
- acquires a process-local reentrant lock;
- acquires the Agent's POSIX advisory file lock with `O_NOFOLLOW`;
- reads the current component generation;
- requires exact expected generation;
- rejects exhausted generation before yielding a mutation window;
- yields to the native mutation operation;
- atomically persists the next generation only after successful completion.

If native mutation raises, the generation does not advance.

The test suite proves both thread-level and real process-level coordination. In the process proof, process A holds generation 0 while process B blocks on the on-disk lock; after A commits generation 1, B resumes and fails `AgentInstanceConflict` rather than overwriting newer durable state.

## Process-restart proof

The normal persistence integration test creates an Agent Instance, writes a learned skill into its Hermes-native profile, advances the skill generation, and launches a completely fresh Python process.

The new process reconstructs `AgentInstanceManager` from only the profiles root/model baseline, reopens the same stable Agent ID/profile, reads the persisted generation, and reads the learned skill. No in-memory Fleet manager state is required.

## Hermes restart and concurrent-use proof

The persistent profile is also exercised through the real Hermes CLI, not only Fleet's profile reader. The integration proof creates the Agent Instance directly under a temporary `HERMES_HOME`, then releases two fresh Hermes processes through a shared barrier so they open the same native profile concurrently.

Both Hermes processes must recognize the exact `fleet-agent-…` profile while Fleet verifies that `config.yaml`, Agent metadata, skill generation, and learned skill bytes remain unchanged. After both exit, a third fresh Hermes process reopens the same profile and the same durable state is verified again. This proves Phase 6 itself does not use persistent config as per-run scratch state and that a Hermes process restart does not erase the durable brain.

Phase 7 still owns the temporary `/v1/runs` execution override; this Phase 6 proof deliberately exercises only concurrent native-profile use and persistent-state isolation.

## Disposable-body/job persistence proof

A separate real-Docker integration proof creates one persistent Agent Instance and learned skill, then creates and destroys two distinct Phase 2 workshop containers with different execution IDs. After each container is gone, Fleet reopens the same Agent binding and verifies the persistent config bytes, Agent metadata, skill generation, and learned skill are unchanged. The two container IDs must differ.

This proves job/body destruction is independent of Agent Instance lifetime without depending on the still-unaudited Phase 8 Run Capsule implementation.

## Real machine-restart proof

Phase 6 no longer defers machine-restart acceptance.

`scripts/run_phase6_machine_restart_proof.py` creates a tiny isolated QEMU/KVM guest using the host's current Linux kernel and a generated initramfs. Katana itself is not rebooted.

The guest receives:

- the host root only as a read-only 9p filesystem so it can execute the exact checked-out Fleet code/system Python;
- one dedicated writable `mapped-xattr` 9p proof directory and no other writable host filesystem.

First guest boot:

1. records `/proc/sys/kernel/random/boot_id`;
2. creates a native persistent Agent Instance on the dedicated persistent share;
3. adds a learned skill;
4. advances the skill generation to 1;
5. persists the Agent identity/proof state;
6. reboots the guest kernel.

Second guest boot:

1. observes a different kernel `boot_id`;
2. creates a fresh `AgentInstanceManager` from disk;
3. reopens the same Agent Instance/profile;
4. verifies skill generation remains 1;
5. verifies the learned skill remains present;
6. powers the guest off.

The lock-order follow-up reran the proof successfully and produced:

- first boot ID: `552d660a-d3c3-463d-badc-85b9981d3ede`;
- second boot ID: `a5246742-b5ce-4c59-a1be-e8b558f38eb4`;
- Agent Instance ID: `sha256:aa011e4fe547f0afa66a0836f13feb2e38c263c9217b06af2ba5e15ba8a580c9`;
- profile: `fleet-agent-aa011e4fe547f0afa66a0836`;
- skills generation: `1`;
- learned skill: `learned before guest reboot`.

The script emits `PHASE6_MACHINE_RESTART_PROOF_OK` only after validating all four guest markers and the persisted proof JSON.

`tests/integration/test_agent_instance_machine_restart_qemu.py` runs the same real reboot proof automatically on Linux x86_64 machines with KVM/QEMU, the current kernel image, BusyBox/cpio, and required 9p modules. Environments without those virtualization prerequisites skip this one hardware-dependent test rather than pretending a process restart is a machine restart.

## Current proof

Original implementation PR **#139** merged as `8061b4f6cf11b6b99187727a5a73cb56bd68fee1`. Re-audit PR **#140** merged as `6f247310a539d12e20535f6d999b3348e1402a37` after all PR checks passed, but its resulting `main` CI run `32074172598` exposed a real concurrency bug in the Hermes clean-install smoke: a second same-generation mutation could validate the state file before acquiring the Agent mutation lock and observe the state file during atomic replacement.

Forward repair PR **#141** moves all state-bearing validation under the Agent state lock for mutation, state reads, Agent reopen, and existing-Agent reuse. Deterministic regressions prove the second mutation and reopen/reuse paths cannot read mutable Agent state until the first mutation releases its lock.

Current lock-order follow-up proof:

- focused Agent Instance + inventory + fresh-process + cross-process + concurrent-Hermes + disposable-body + QEMU machine-restart suite: **55 passed**;
- full Fleet Python suite on the verified local Hermes runtime: **901 passed**;
- deterministic state-lock ordering regressions: PASS;
- real concurrent Hermes profile-use/restart proof: PASS;
- two distinct real-Docker workshop lifecycles with one persistent Agent Instance: PASS;
- repository Ruff lint: PASS;
- `git diff --check`: PASS at the pre-PR gate;
- public-hygiene scan: PASS at the pre-PR gate;
- repeatable isolated machine-restart script: `PHASE6_MACHINE_RESTART_PROOF_OK`.

The known machine-global Hermes launcher contamination is avoided by placing Fleet's verified `.venv/bin` first on `PATH` for local full-suite validation. No product code is changed to accommodate that launcher.

No Hermes Agent repository change is required in Phase 6. The durable brain deliberately uses Hermes' existing native profile/config/memory/skill substrate. Phase 7 is the Agent-side change that carries temporary Fleet execution binding through the native Runs path without rewriting this persistent config.

## Phase 6 closure

Phase 6 is closed only when all of the following remain true on canonical Fleet `main`:

1. stable Agent identity is independent of run/container/base-version identity;
2. Agent state is a native persistent Hermes profile and is never normal run-cleanup state;
3. temporary execution/authority state is excluded from persistent files/config;
4. immutable Agency base bytes cannot drift while Fleet continues advertising the old base identity;
5. learned native overlay state survives exact-base reuse;
6. memory/skill generations serialize across threads and processes;
7. state-bearing mutation/read/reopen/reuse validation occurs under the same Agent lock;
8. concurrent Hermes processes can use the same native Agent profile without persistent config collisions;
9. a fresh Hermes process reopens the same Agent profile with durable learning intact;
10. destruction of distinct disposable workshop bodies does not change Agent identity/config/learning;
11. persistent control state fails closed on unsafe ownership/mode/link/path state;
12. fresh Fleet-process persistence proof passes;
13. real isolated machine-restart proof passes with two distinct kernel boot IDs and preserved learning;
14. required PR CI and resulting `main` CI are green.

## Explicit non-goals retained for later phases

Phase 6 does not yet:

- send Fleet container/runtime binding through Hermes `/v1/runs`: Phase 7;
- orchestrate complete Run Capsule lifecycle/recovery: Phase 8;
- establish formal principal identity: Phase 9;
- issue/verify complete immutable RunAuthority: Phase 10;
- implement principal-scoped memory retrieval: Phase 11;
- implement full base+learned-overlay upgrades: Phase 24;
- remove the legacy disposable-profile execution path: Phase 37.

The durable Agent brain is now acceptance-proven independently of the temporary body and authority that later phases attach to it.
