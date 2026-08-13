# Docker OCI execution backend

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

Before creating a container, Fleet inspects the local image and proves both its digest and labels binding the exact immutable Agency repository, revision, profile, version, and content digest from `ResolvedRecipe`. A plain image tag or an image missing that identity fails closed.

The created container uses:

- a read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- no network;
- bounded CPU, memory, swap, and PIDs;
- no Docker log driver;
- Fleet ownership labels that bind execution, idempotency digest, Recipe digest, capability digest, and backend kind.

No credentials, environment variables, host paths, volumes, privileged mode, host namespace sharing, or Docker socket mounts enter this contract.

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

This backend does not authorize execution, select a node, schedule work, submit Keryx tasks, install persistent Agency profiles, or replace Hermes Agent. It realizes an already approved exact plan on a destination selected by higher layers. Keryx remains the durable task/result/artifact ledger and Hermes Agent remains the execution owner.

Exact-node Recipe orchestration is a later milestone. This module is the concrete runtime boundary it can call after Fleet authorization, readiness, destination admission, and exact ingredient resolution have succeeded.
