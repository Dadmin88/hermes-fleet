# vNext Phase 21: Disposable Templar sandbox

Status: **CLOSURE-GATED**

Phase 21 gives the Phase 20 Templar core a real disposable Linux evaluator runtime. It does not add any execution authority. Templar still returns only `ALLOW`, `DENY`, or `REVIEW`, every verdict still carries `authority: none`, and deterministic Fleet deny still wins.

The Phase 21 boundary is:

```text
exact Phase 20 evaluation request
        |
        v
fresh Bubblewrap evaluator sandbox
        |
        +-- optional dedicated provider proxy FD
        |
        v
closed Phase 20 backend response
        |
        v
Phase 20 verdict validation / fail-closed handling
```

Every evaluation gets a new process, mount namespace, PID namespace, user namespace, network namespace, tmpfs state, evaluator artifact staging FD, and optional fresh provider channel. Nothing from the evaluator runtime is reused as state for the next evaluation.

## Ownership boundary

### Fleet/Templar owns in Phase 21

- creating and destroying one disposable evaluator sandbox per evaluation;
- exact evaluator-artifact hash verification before launch;
- staging evaluator source through an anonymous file descriptor rather than exposing the source checkout;
- a minimal read-only system-Python runtime surface;
- unprivileged UID/GID and zero effective capabilities;
- hard CPU, address-space, file-size, open-file, process-count, core-dump, and wall-clock limits;
- a completely unshared IP network namespace;
- an empty/allowlisted environment;
- bounded stdin/stdout/stderr handling;
- hard process-tree termination on evaluator timeout;
- optional delivery of one already-connected credential-free provider proxy channel;
- destroying all evaluator-local state after the verdict path completes or fails.

### Phase 21 does not own

- model/provider routing itself; Hermes Agent remains the provider-routing owner;
- Vault secret bodies or provider credentials;
- Fleet, Keryx, Nodescale, Docker, SSH, terminal, or host-broker authority;
- normal Hermes Agent memory or skill-writing primitives;
- Phase 22 pre-execution gate ordering;
- Phase 23 learning/promotion gating;
- Phase 26 durable audit/provenance storage.

## Runtime primitive

The Linux runtime is Bubblewrap, matching the already-proven Phase 17 disposable verification primitive.

Each evaluator launch uses:

- `--unshare-all`;
- `--die-with-parent`;
- UID/GID `65534`;
- `--cap-drop ALL`;
- a synthetic `/usr/bin` containing only system Python;
- read-only system runtime libraries needed by that Python interpreter;
- private `/proc` and `/dev` views;
- tmpfs `/tmp` and `/work`;
- no host home, project tree, workspace, `/run`, `/var`, `/media`, `/mnt`, or management-socket mount;
- `clearenv` plus a bounded runtime allowlist;
- a verified evaluator artifact copied into `/app/evaluator.py` through Bubblewrap `--ro-bind-data` from an anonymous file descriptor.

The master-plan rule “no host mounts” is enforced as **no host data/project/state mounts**. The only read-only host-backed material visible in the namespace is the minimal system runtime needed to start the fixed Python interpreter. This follows the Phase 17 Bubblewrap precedent, whose runtime policy likewise describes the host filesystem as not mounted while exposing read-only system runtime libraries. No user data, repository, configuration, credential, socket directory, Agent state, or arbitrary host path is mounted.

The evaluator source path itself is never mounted. Fleet opens the exact configured regular file with no-follow semantics, bounds its size, verifies its SHA-256 identity, copies those exact bytes into an anonymous temporary file descriptor, and Bubblewrap injects those bytes read-only into the sandbox. Symlinked evaluator source fails closed.

## Exact Phase 20 request binding

The sandbox backend independently reconstructs the Phase 20 `TemplarEvaluationRequest` before launch.

It requires the exact closed request schema and revalidates:

- request hash;
- event hash;
- Fleet policy digest;
- Templar policy identity;
- evaluator/model identity;
- issue/deadline timestamps;
- complete Phase 19 event binding;
- deterministic `evaluation_id`.

A forged `evaluation_id`, extra request field, stale/malformed Phase 19 event binding, or mismatched evaluator identity is rejected before Bubblewrap starts.

This duplicates the most important Phase 20 checks at the Phase 21 process boundary so a direct backend caller cannot bypass the core’s exact-request contract.

## No secret bodies

Phase 21 receives the already-sanitized Phase 19/20 request contract, not arbitrary prompt/context state.

Defense in depth rejects known secret-bearing field names before sandbox launch. The real runtime proof also verifies that representative host-side credential/routing variables do not cross the `clearenv` boundary, including Keryx, Vault, SSH-agent, Docker, Hermes-home, and Fleet test credential material.

The sandbox receives no Vault body, API credential, normal Agent profile, persistent memory, skill directory, or host home.

If future provider routing needs a secret, that secret remains outside the sandbox in the provider-routing owner. Only the provider result may cross the dedicated provider channel.

## No management authority

The namespace exposes no:

- Docker socket;
- Fleet socket;
- Keryx socket or credential;
- Hermes management socket/state tree;
- SSH executable or SSH agent;
- shell executable in the synthetic command path;
- terminal/tool broker;
- host-action broker;
- normal Agent memory/skill API.

`/run` and `/var/run` are absent. `/home` is absent. The synthetic `/usr/bin` contains only system Python.

This means model/evaluator output cannot discover a management socket path and cannot invoke an arbitrary host command even if evaluator code attempts to do so.

## Provider access

Default policy is `provider_access: none`.

When provider access is explicitly required, Phase 21 permits only `provider-channel` mode. The sandbox still has an unshared IP network namespace. The trusted host integration may pass one already-connected `AF_UNIX` stream FD into the evaluator. No provider hostname, host network, credential, management-socket directory, or secret body is exposed.

`ProviderChannelFactory` is deliberately a trusted host wiring seam. Its contract is strict: the returned channel must be a fresh, credential-free connection dedicated to model/provider routing for that one evaluation. Supplying a Fleet, Keryx, Docker, Hermes-management, or other authority-bearing connection would violate the Phase 21 wiring contract. Fleet itself does not open arbitrary Unix-socket paths on behalf of evaluator output.

The real integration proof uses a fresh socketpair-backed fake provider proxy and proves simultaneously that the AF_UNIX channel works while direct IPv4 egress remains unavailable.

## Resource bounds

`fleet.templar-sandbox-policy.v1` is frozen and content-addressed. Default limits are:

- wall clock: 5 seconds;
- CPU: 2 seconds;
- virtual address space: 256 MiB;
- file size: 64 KiB;
- open files: 64;
- processes: 16;
- stdout: 64 KiB;
- stderr: 16 KiB;
- core dumps: disabled.

The hard POSIX limits are lowered inside the fixed Fleet-owned Python harness before evaluator source is executed. Evaluator code cannot raise those hard ceilings afterward. The integration suite proves both the visible hard limits and a failed attempt to widen the CPU ceiling.

A real 512 MiB allocation attempt is denied under the default 256 MiB address-space ceiling.

## Hard timeout and process destruction

Wall-clock timeout is enforced by the parent outside the sandbox.

On timeout Fleet:

1. sends `SIGKILL` to the Bubblewrap process group;
2. waits for teardown;
3. relies additionally on Bubblewrap `--die-with-parent` and the private PID namespace;
4. returns `TemplarSandboxTimeout`;
5. lets Phase 20 convert that timeout to `DENY`, origin `core-fail-closed`, reason `evaluator-timeout`.

The real integration test makes evaluator code spawn a child Python process and then hang. After the timeout, the test scans host `/proc` for a unique child marker and proves that no descendant remains.

This is the hard lifecycle guarantee intentionally deferred by Phase 20.

## Bounded protocol

Input is one canonical Phase 20 request on stdin, bounded before launch.

Output must be one JSON object using the closed Phase 20 backend-response schema. Unknown/malformed JSON, nonzero evaluator exit, oversized output, startup failure, unavailable Bubblewrap, invalid evaluator artifact, or timeout all fail closed.

Raw evaluator stderr and exception text are not persisted into Templar verdicts.

## Freshness and no cross-evaluation state

Writable state exists only in tmpfs `/tmp` and `/work` for the lifetime of one Bubblewrap process.

The integration suite writes a marker during one evaluation and immediately performs another evaluation using the same backend object. The second sandbox proves the marker does not exist.

The runtime therefore provides no persistent Agent memory, no persistent skill-writing location, and no reusable evaluator scratch state.

## Escape and denial proof

The explicit Phase 21 Bubblewrap integration suite proves:

- non-root UID/GID;
- zero effective capabilities;
- clearenv allowlist;
- representative host credential/routing variables absent;
- no `/home`, `/root`, `/run`, `/var`, `/media`, `/mnt`, or workspace path;
- no Docker/Fleet/Hermes management socket path;
- no SSH or shell executable;
- no pre-existing Hermes memory/skill state;
- private writable tmpfs only;
- hard resource ceilings visible and non-widenable;
- direct IPv4 network access denied;
- actual over-limit memory allocation denied;
- provider-only AF_UNIX channel works when explicitly enabled;
- state disappears between evaluations;
- timeout kills evaluator descendants;
- malformed/nonzero evaluator results fail closed;
- hard timeout maps through Phase 20 to a fail-closed Templar `DENY`.

## CI proof

Fleet CI already installs Bubblewrap and preflights unprivileged user namespaces in the pinned-Hermes clean-install job for the Phase 17 verification seam.

Phase 21 extends that same proven job with an explicit:

```text
FLEET_TEMPLAR_SANDBOX_INTEGRATION=1
python -m pytest tests/unit/test_templar_sandbox.py -q
```

The ordinary quality matrix keeps kernel-backed integration tests opt-in so a runner without a preflighted user-namespace environment does not accidentally turn “Bubblewrap binary exists” into a false security proof. The dedicated CI step runs them only after Bubblewrap/user-namespace setup has succeeded.

## Local acceptance evidence

Current Phase 21 pre-PR evidence:

- structural/unit Phase 21 suite: 8 passed, 6 explicit integration skips;
- explicit real Bubblewrap Phase 21 suite: 14 passed;
- Phase 19/20/21 security regression chain: 62 passed, 6 explicit Bubblewrap skips;
- full Fleet suite excluding the separately pinned Hermes plugin smoke: 1027 passed, 18 environment/integration skips on the final rerun;
- one earlier broad run hit the existing timing-sensitive HermesRuns stop-confirmation test once; the exact test passed immediately in isolation and the complete suite then reran green without code changes to that subsystem;
- exact Phase 20 request reconstruction and evaluation-ID revalidation: green;
- evaluator-artifact hash mismatch: fail closed;
- evaluator-artifact symlink: fail closed;
- secret-bearing request defense in depth: green;
- non-root / zero-capability / `NoNewPrivs=1` runtime: green;
- host path/socket denial: green;
- SSH/shell denial: green;
- Keryx/Vault/SSH-agent/Docker/Hermes environment denial: green;
- resource-limit visibility and hard-limit non-widenability: green;
- real address-space exhaustion denial: green;
- direct IP network denial: green;
- provider-only anonymous AF_UNIX socketpair proof: green;
- named Unix socket provider handoff: rejected before launch;
- fresh-state/no-cross-evaluation proof: green;
- hard timeout process-tree destruction: green;
- Phase 20 fail-closed timeout mapping: green;
- malformed/nonzero evaluator output: fail closed;
- full Ruff lint across `hermes_fleet`, dashboard, scripts, and tests: green;
- formatter compatibility for new Phase 21 Python files: green;
- public-hygiene scan: green;
- Desktop plugin JavaScript syntax check: green;
- Python sdist/wheel build: green and includes `hermes_fleet/templar_sandbox.py`;
- Rust `cargo fmt --all -- --check`: green;
- Rust `cargo clippy --workspace --all-targets -- -D warnings`: green;
- Rust `cargo test --workspace`: green;
- Rust `cargo build --workspace`: green;
- `git diff --check`: green.

Repository closure still requires the normal path:

1. reviewable branch/commit;
2. exact final PR-head CI green, including the explicit Phase 21 Bubblewrap proof;
3. normal GitHub merge;
4. exact resulting `main` CI green;
5. closure record update through the normal closure workflow.

Until those steps are complete, Phase 21 remains `CLOSURE-GATED` rather than `COMPLETE`.

## Later-phase boundary

Phase 21 does **not** implement:

- Phase 22 pre-execution Templar gate ordering;
- Phase 23 learning/promotion Templar gate;
- Phase 26 full durable audit/provenance chain;
- Phase 35 operator Templar UX.

Phase 22 may compose the Phase 19 event, deterministic Fleet policy, Phase 20 core, and Phase 21 disposable sandbox into the pre-execution gate. Phase 21 itself does not issue RunAuthority, create a Run Capsule, start execution, or make Fleet’s final authorization decision.
