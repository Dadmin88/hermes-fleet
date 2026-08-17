# Hermes Fleet vNext Phase 2 acceptance: disposable OCI runtime body

Status: **COMPLETE**

Phase 2 establishes the disposable OCI container as temporary execution body only. Fleet owns the container lifecycle. Hermes Agent remains outside as the durable brain and may only enter an exact Fleet-owned workshop after independent verification.

This acceptance record is scoped to the operator-supplied Phase 2 contract. Later phases still own richer workspace grants, network policy, Run Capsule orchestration, persistent Agent Instances, and immutable RunAuthority.

## Ownership and compatibility

| Requirement | Evidence | Result |
| --- | --- | --- |
| Reuse Fleet's mature OCI/Docker backend | `hermes_fleet/oci_backend.py` retains `DockerExecutionBackend` and adds the workshop as a specialization. | PASS |
| Fleet owns lifecycle | `DockerWorkshopBackend.ensure/find/cleanup_plan` create/start/stop/remove through Fleet only. Hermes' Phase 2 environment exposes inspect + `docker exec`, not lifecycle mutation. | PASS |
| Dedicated workshop / Run Capsule backend | `DockerWorkshopBackend` is the dedicated disposable tooling body. | PASS |
| Preserve old OCI semantics for legacy callers | Legacy `DockerExecutionBackend` still requires exact Agency labels in its image; existing legacy unit/integration behavior remains green. | PASS |
| Agency profile is not a container-image requirement | Workshop overrides image-label requirements and accepts a generic digest-pinned tooling image. | PASS |
| Container is tooling/runtime only | Workshop carries runtime/tooling state and tmpfs only; no Hermes profile, memory, skill, session, or Agency identity is mounted into it. | PASS |
| Hermes Agent stays outside | Agent enters the workshop only through an attach-only `docker exec` environment after independent inspection. | PASS |

## Container hardening

| Requirement | Evidence | Result |
| --- | --- | --- |
| Digest-pinned image | `OciRealizationSpec` rejects tags and accepts only exact sha256 image identity. | PASS |
| Local digest verification | Fleet performs `docker image inspect` and verifies the requested digest before create. | PASS |
| Non-root | Workshop uses and verifies the exact dedicated identity `65532:65532`; Agent independently requires the same exact non-root UID/GID pair. | PASS |
| `CapDrop=ALL` | Create argv and observed Docker `HostConfig.CapDrop` are verified. | PASS |
| No added capabilities | Fleet and Agent both reject non-empty `CapAdd`. | PASS |
| `no-new-privileges` | Fleet create + observed inspection and Agent inspection require it. | PASS |
| Read-only root filesystem | Fleet create + observed inspection and Agent inspection require `ReadonlyRootfs=true`. | PASS |
| PID limit | Positive configured PID limit is created and verified by Fleet and Agent. | PASS |
| RAM limit | Positive memory + memory-swap limit is created and verified. | PASS |
| CPU limit | Positive CPU limit is created and verified via Docker `NanoCpus`. | PASS |
| Deadline | Workshop is bound to an absolute `dev.hermes.fleet.deadline_ms`; Fleet refuses prepare/start at or after expiry; Agent independently rejects expired workshops. Active deadline supervision/teardown belongs to Phase 8. | PASS |
| `network=none` initially | Fleet creates and both Fleet/Agent verify `NetworkMode=none`. | PASS |
| No Docker socket | No bind/volume mounts are permitted; observed persistent mounts fail closed; `DOCKER_*` environment authority is rejected independently by Fleet and Agent. | PASS |
| No `/` host mount | No bind mounts are permitted, and incomplete mount inspection fails closed. | PASS |
| No host home | No bind mounts are permitted; workshop home is bounded tmpfs `/home/fleet`, and the observed `HOME` identity must match exactly. | PASS |
| No SSH credentials | Fleet creates only bounded `HOME`/`TMPDIR` runtime env and both Fleet/Agent reject SSH-prefixed and credential-like observed environment authority. | PASS |
| No Fleet/Keryx/Nodescale sockets | No bind/volume mounts are permitted and Fleet/Keryx/Nodescale environment authority is rejected. | PASS |
| No unrestricted host mounts | Fleet and Agent reject bind mounts, persistent volumes, host devices, and device requests; missing mount inspection fails closed. | PASS |

## Ownership identity

Every workshop is bound to the existing exact-plan ownership chain:

- execution ID: `dev.hermes.fleet.execution`;
- capability hash: `dev.hermes.fleet.capabilities`;
- idempotency digest: `dev.hermes.fleet.idempotency`;
- resolved Recipe hash: `dev.hermes.fleet.recipe`;
- exact plan fingerprint: `dev.hermes.fleet.plan`;
- Fleet backend label: `dev.hermes.fleet.backend=fleet.dev/docker-oci`;
- workshop role: `dev.hermes.fleet.role=workshop`;
- absolute deadline: `dev.hermes.fleet.deadline_ms`.

The plan fingerprint is independently recoverable from the core execution/capability/idempotency/Recipe ownership labels. Fleet additionally requires the explicit plan label to equal that observed fingerprint.

## Independent verification

Fleet validates the observed Docker document during ownership checks. It does not infer security from the create command.

Hermes Agent's Phase 2 attach-only primitive independently verifies before attachment and before every command:

- exact full Docker container ID;
- exact Fleet backend label;
- exact plan fingerprint;
- workshop role;
- future deadline;
- running state;
- `network=none`;
- read-only root;
- exact dedicated non-root user/group `65532:65532`;
- unprivileged container with explicit `Privileged=false`;
- `CapDrop=ALL`;
- no added capabilities;
- `no-new-privileges` and no `unconfined` security option;
- positive CPU/RAM/PID limits;
- no bind mounts or persistent volumes;
- no host device/device-request grants;
- exact `HOME=/home/fleet` and `TMPDIR=/tmp` runtime identity with no forbidden control/credential environment authority;
- writable tmpfs `/workspace`.

Hermes then uses only `docker exec` against that exact ID. The attach-only environment has no create, replacement, restart, stop, remove, discovery, or fallback path. A missing or weakened exact container fails closed.

## Tests and canonical proof

### Fleet

Phase 2 Fleet hardening is canonical on `Dadmin88/hermes-fleet` `main` through PR #133, merge commit `7f22eaf61c3c119378b2009b18ae43351e33a26f`.

Current proof:

- complete local Fleet suite with the CI-pinned Hermes test runtime: **863 passed**;
- Phase 2 OCI unit + real-Docker focused suite: **50 passed**;
- real Docker workshop proof covers generic image use, exact ownership labels, dedicated non-root identity, `network=none`, read-only root, dropped/no-added capabilities, no-new-privileges, exact CPU/RAM/PID limits, bounded tmpfs home/workspace, mount-free posture, deadline binding, and zero container residue;
- observed-container recovery/adoption rejects forbidden control/credential environment authority, ambiguous privilege state, host devices/device requests, missing mount inspection, and persistent mounts;
- Ruff: PASS;
- `git diff --check`: PASS;
- public repository hygiene: PASS;
- PR #133 required CI: PASS;
- post-merge Fleet `main` CI run `32037714766`: PASS across Python 3.11, Python 3.13, Rust workspace compatibility, real Nodescale/readiness proofs, and Hermes plugin clean-install smoke.

### Hermes Agent

Hermes' independent Phase 2 verifier is canonical on `Dadmin88/hermes-agent` `main` through PR #2, merge commit `59545071b1ea56ab855ffc02cfc0b31938df00cf`.

The original `vnext/phase2-disposable-oci-body` commit `ef20e27ee` is historical source evidence only. Its Phase 2 verifier was replayed onto the Phase 1-canonical Agent `main`, then hardened before merge.

Current proof:

- preserved Phase 1 + Phase 2 local regression slice: **133 passed, 2 platform skips**;
- Phase 2 exact-workshop verifier + real-Docker entry proof: **32 passed**;
- Hermes requires the exact full container ID, exact Fleet backend/plan identity, live deadline, running state, exact dedicated non-root UID/GID, `network=none`, read-only root, unprivileged state, dropped/no-added capabilities, no-new-privileges, positive CPU/RAM/PID limits, no persistent mounts/devices, bounded credential-free environment authority, and writable tmpfs workspace;
- explicit missing-container test proves no fallback or replacement path exists;
- real Docker proof executes inside the exact workshop and proves Hermes cleanup leaves the Fleet-owned container running;
- Ruff: PASS;
- `git diff --check`: PASS;
- PR #2 required CI: PASS after the fork's upstream `ci-reviewed` label policy was satisfied for the reviewed three-file diff;
- post-merge Agent `main` CI run `32037187739`: PASS;
- post-merge Agent Docker build/test workflow `32037187074`: PASS.

The two local skips are pre-existing platform-conditional Phase 1 cases; the corresponding Windows/macOS CI lanes passed.

### Cross-repository ownership proof

A live cross-repository Docker proof used the same exact container for both owners:

1. Fleet created and independently verified the digest-pinned workshop from an exact `ExecutionPlan`;
2. Hermes independently inspected the same full container ID and plan fingerprint;
3. Hermes entered it only through `docker exec` and observed UID `65532` and `/workspace`;
4. `FleetWorkshopEnvironment.cleanup()` left the container running;
5. Fleet alone destroyed the workshop;
6. a new Hermes attachment attempt against that destroyed exact ID failed closed;
7. no Fleet workshop container residue remained.

Proof result: `PHASE2_CROSS_REPO_DOCKER_PROOF_OK`.

## Phase 2 closure rule

Phase 2 is closed only when the Fleet workshop implementation is on canonical Fleet `main` with green CI, the independent Hermes verifier is on canonical Agent `main` with green CI, the live cross-repository ownership proof succeeds, and the Phase 1 reliability baseline remains green. Historical branches alone cannot satisfy closure.

## Explicit non-goals retained for later phases

Phase 2 does not prematurely implement:

- project/host workspace mounts or export policy: Phase 3;
- controlled network egress: Phase 4;
- host-action broker: Phase 5;
- persistent Agent Instance lifecycle: Phase 6;
- `fleet_runtime` `/v1/runs` binding: Phase 7;
- active Run Capsule deadline supervision and guaranteed teardown: Phase 8;
- principal identity / RunAuthority / scoped secrets / memory / learning: later numbered phases.

This keeps the container disposable and low-authority while preserving the numeric master-plan order.
