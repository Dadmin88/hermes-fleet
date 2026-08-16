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
- writable bounded tmpfs at `/workspace`, `/tmp`, and `/home/fleet`;
- only `HOME=/home/fleet` and `TMPDIR=/tmp` injected by Fleet;
- a future absolute deadline; prepare/start fail once the deadline is reached;
- idempotent find/ensure/cleanup that never creates a replacement during cleanup or recovery.

Fleet verifies the observed Docker document after realization, rather than trusting its create arguments. Hermes Agent has a separate attach-only verifier: it independently re-inspects the exact full container ID and exact plan fingerprint, requires the same hardening posture and a future deadline, and then may use only `docker exec`. Hermes has no create/start/stop/remove fallback in that path. Missing or weakened exact state fails closed.

Active deadline supervision and guaranteed teardown are owned by the later Run Capsule lifecycle; Phase 2 already binds the execution body to the deadline and forbids creation or start after expiry.

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
