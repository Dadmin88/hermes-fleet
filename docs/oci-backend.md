# Docker OCI execution backend

> **vNext reuse note:** this shipped backend is an existing mature Fleet OCI primitive. vNext reuses and hardens that primitive for the disposable execution body, but this document does **not** by itself describe the complete vNext workshop/Run Capsule contract. See [vNext foundation](vnext-foundation.md).

`DockerExecutionBackend` is the first concrete implementation of the provider-neutral [`ExecutionBackend`](execution-backend.md) lifecycle. It delegates process isolation and resource enforcement to the mature Docker OCI runtime; Fleet does not implement a container runtime.

```text
ResolvedRecipe
  → policy-approved ExecutionPlan
  → exact Docker OCI realization
  → opaque backend handle
```

## Bounded first slice

The backend accepts an `OciRealizationSpec` only after an upstream owner has selected an exact runtime image and command. That provider-specific spec requires:

- an image pinned by exact OCI content digest (`sha256:…`) or repository digest;
- a bounded argument vector with no secret-looking assignments;
- disabled container networking;
- explicit CPU, memory, memory-swap, and PID limits.

Before creating a legacy `DockerExecutionBackend` container, Fleet inspects the local image and proves both its digest and labels binding the exact immutable Agency repository, revision, profile, version, and content digest from `ResolvedRecipe`. A plain image tag or an image missing that identity fails closed. This legacy behavior is preserved for existing callers.

The vNext `DockerWorkshopBackend` deliberately does **not** require Agency identity in the image. Its image is a generic digest-pinned tooling/runtime body. The persistent Hermes Agent Instance remains outside the container and carries the Agency identity, memory, skills, sessions, and other durable brain state.

The created container uses:

- a read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- no network;
- bounded CPU, memory, swap, and PIDs;
- no Docker log driver;
- Fleet ownership labels that bind execution, idempotency digest, Recipe digest, capability digest, and backend kind.

No credentials, environment variables, host paths, volumes, privileged mode, host namespace sharing, or Docker socket mounts enter this legacy contract.

## vNext disposable workshop

`DockerWorkshopBackend` specializes the mature backend without weakening legacy semantics. Fleet remains the sole lifecycle owner. The workshop is bound to the exact execution ID, capability hash, idempotency digest, resolved Recipe hash, plan fingerprint, backend kind, role, and absolute deadline.

The workshop adds and verifies:

- numeric non-root UID/GID `65532:65532`;
- generic digest-pinned tooling image, independent of Agency profile identity;
- `network=none`;
- read-only root filesystem;
- `CapDrop=ALL` with no added capabilities;
- `no-new-privileges` and no unconfined security option;
- positive CPU, RAM, swap, and PID limits;
- no bind mounts, named volumes, host devices, Docker socket, host home, SSH state, or Fleet/Keryx/Nodescale sockets;
- writable bounded tmpfs at `/workspace`, `/workspace/inputs`, `/tmp`, and `/home/fleet`;
- Agent workspace ownership `65532:65532` and a distinct non-root input-staging identity `65533:65533`;
- only `HOME=/home/fleet` and `TMPDIR=/tmp` injected by Fleet;
- a future absolute deadline; prepare/start fail once the deadline is reached;
- idempotent find/ensure/cleanup that never creates a replacement during cleanup or recovery.

Fleet verifies the observed Docker document after realization, rather than trusting its create arguments. Hermes Agent has a separate attach-only verifier: it independently re-inspects the exact full container ID and exact plan fingerprint, requires the same hardening posture and a future deadline, and then may use only `docker exec`. Hermes has no create/start/stop/remove fallback in that path. Missing or weakened exact state fails closed.

Active deadline supervision and guaranteed teardown are owned by the later Run Capsule lifecycle; Phase 2 already binds the execution body to the deadline and forbids creation or start after expiry.

## vNext workspace isolation

Phase 3 keeps project data out of the container by default. Fleet does not mount the host working directory, host home, or arbitrary host paths. Instead, a destination has an explicit map of trusted project IDs to canonical project roots, and filesystem requests contain only a project ID, relative path, `/workspace` target, byte bound, and immutable authority hashes.

Before any filesystem authority becomes effective, Fleet canonicalizes the requested source and proves it remains inside the configured project root and outside forbidden host state. Symlink escapes, traversal, special files, sensitive credential/state components, broad host roots, Docker sockets, and Fleet/Keryx/Nodescale/Vault state are rejected.

Filesystem projections are deliberately stricter than host bind mounts:

- read authority targets only `/workspace/inputs/...`;
- read input is copied into the per-run tmpfs by staging UID/GID `65533:65533`, mode-stripped, and therefore cannot be written or chmod-restored by the Agent running as `65532:65532`;
- write authority targets only `/workspace/work/...` and requires a separate write-authority hash in addition to the RunAuthority hash;
- the writable projection is still a disposable copy, not a writable host bind;
- at most eight projections are accepted, each input is byte-bounded, and aggregate staged input bytes are bounded;
- the raw host path never appears in a Recipe/runtime grant.

`FilesystemAuthorityScope` is the Phase 3 enforcement adapter for the later immutable `RunAuthority`. It does not implement Phase 10 early: it requires one verified RunAuthority hash plus an explicit set of separately approved write-authority hashes. Phase 10 will become the producer of that verified scope.

Artifacts use an explicit copy-out path rather than a mounted host output directory. Only declared `/workspace/out` paths can be exported. Fleet bounds export count and bytes, rejects links, traversal, special entries, and oversized archives, and can require an output scanner before an artifact is accepted. The export API returns validated bytes to higher layers; it does not silently write arbitrary host paths.

The whole workspace remains tmpfs/container state. Fleet container cleanup destroys it. The real-Docker Phase 3 proof destroys run N and demonstrates that a fresh run N+1 cannot observe run N input copies, writable copies, declared outputs, or undeclared temporary files. Phase 8 owns the higher-level ordering guarantee that finalization/quiescence occurs before this destruction.

Hermes independently checks the workshop's Agent UID and the distinct input-staging tmpfs ownership before attachment. It still has no lifecycle or staging-identity fallback.

## vNext network isolation

Phase 4 keeps `network=none` as the default and preserves `provider-only` as an offline workshop posture: model-provider traffic stays outside the container. Direct workshop egress is possible only for an exact `project-allowlist` or `explicitly-approved-internet` grant bound to the verified RunAuthority hash; the latter additionally requires a separate approval hash and still carries exact pinned destinations.

Direct modes place the workshop on a per-execution Docker `--internal` network, not the ordinary bridge. Direct DNS is disabled and the workshop has no normal external route, so ignoring proxy environment variables cannot create raw internet/LAN access. A disposable, hardened Fleet egress gateway is the only component attached to both the internal network and an outbound bridge.

The gateway resolves destinations itself and requires every runtime public-IPv4 answer set to equal the authorization-time pinned set before opening a CONNECT tunnel. Special/private/LAN/shared-management/metadata destinations and direct DNS or Docker-management ports fail closed. Fleet continuously verifies exact internal-network membership, so an unexpected lateral peer invalidates the enforcement state.

The gateway starts with only the internal network attached, binds its listener to Docker's assigned internal IPv4 address, and is independently inspected before Fleet attaches its outbound bridge. Fleet re-verifies the listener afterward to prove it did not widen to another address or IPv6. The exact gateway image, startup command, generated policy/script hashes, non-root hardening, resource bounds, mount posture, bounded audit-log configuration, and network attachments are all checked from the observed Docker document.

Hermes Agent independently verifies the expected Phase 4 binding before attachment and before each command. Offline/provider-only runs must still be `network=none`. Mediated runs must match the exact Fleet network, policy hash, authority hash, gateway ID/IP, loopback-only DNS posture, proxy environment, and single-network membership. Hermes still enters only the exact Fleet-owned container via `docker exec` and receives no network-lifecycle authority.

See [Phase 4 network isolation](vnext-phase4-network-isolation.md) for the full requirement-by-requirement acceptance evidence.

## Lifecycle and recovery

Container names are deterministic digests of Fleet execution identity. `prepare` inspects before create and after any uncertain create response. It adopts an existing realization only when all exact ownership labels and the image identity match; otherwise it rejects the collision without mutation.

`start`, `inspect`, `stop`, and `cleanup` re-inspect and verify ownership before mutation. Response-loss recovery never issues an unbounded duplicate create or start. Cleanup removes only a container carrying the expected Fleet ownership evidence and is idempotent when it is already absent.

Docker states map to the shared lifecycle:

- `created` → `prepared`;
- `running`, `paused`, or `restarting` → `running`;
- `exited` with zero status → `completed`;
- `exited` nonzero → `failed`;
- `dead` or `removing` → `indeterminate`.

Unsupported or malformed inspection documents are unavailable rather than guessed.

## Authority boundary

This backend does not authorize execution, select a node, schedule work, submit Keryx tasks, install persistent Agency profiles, or replace Hermes Agent. It realizes an already approved exact plan on a destination selected by higher layers. For cross-machine work, Keryx remains the durable remote task/result/artifact ledger. Same-machine execution does not require Keryx, and Hermes Agent remains the local execution owner.

Exact-node Recipe orchestration is a later milestone. This module is the concrete runtime boundary it can call after Fleet authorization, readiness, destination admission, and exact ingredient resolution have succeeded.
