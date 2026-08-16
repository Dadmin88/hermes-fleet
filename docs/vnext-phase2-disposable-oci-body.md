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
| Non-root | Workshop uses and verifies `65532:65532`; Agent independently rejects root/empty UID. | PASS |
| `CapDrop=ALL` | Create argv and observed Docker `HostConfig.CapDrop` are verified. | PASS |
| No added capabilities | Fleet and Agent both reject non-empty `CapAdd`. | PASS |
| `no-new-privileges` | Fleet create + observed inspection and Agent inspection require it. | PASS |
| Read-only root filesystem | Fleet create + observed inspection and Agent inspection require `ReadonlyRootfs=true`. | PASS |
| PID limit | Positive configured PID limit is created and verified by Fleet and Agent. | PASS |
| RAM limit | Positive memory + memory-swap limit is created and verified. | PASS |
| CPU limit | Positive CPU limit is created and verified via Docker `NanoCpus`. | PASS |
| Deadline | Workshop is bound to an absolute `dev.hermes.fleet.deadline_ms`; Fleet refuses prepare/start at or after expiry; Agent independently rejects expired workshops. Active deadline supervision/teardown belongs to Phase 8. | PASS |
| `network=none` initially | Fleet creates and both Fleet/Agent verify `NetworkMode=none`. | PASS |
| No Docker socket | No bind/volume mounts are permitted; observed persistent mounts fail closed. | PASS |
| No `/` host mount | No bind mounts are permitted. | PASS |
| No host home | No bind mounts are permitted; workshop home is bounded tmpfs `/home/fleet`. | PASS |
| No SSH credentials | No host mounts or arbitrary host environment forwarding enter the workshop; Fleet injects only `HOME` and `TMPDIR`. | PASS |
| No Fleet/Keryx/Nodescale sockets | No bind/volume mounts are permitted. | PASS |
| No unrestricted host mounts | Fleet and Agent reject bind mounts and persistent volumes; Agent also rejects host devices/device requests. | PASS |

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
- non-root user;
- unprivileged container;
- `CapDrop=ALL`;
- no added capabilities;
- `no-new-privileges` and no `unconfined` security option;
- positive CPU/RAM/PID limits;
- no bind mounts or persistent volumes;
- no host device/device-request grants;
- writable tmpfs `/workspace`.

Hermes then uses only `docker exec` against that exact ID. The attach-only environment has no create, replacement, restart, stop, remove, discovery, or fallback path. A missing or weakened exact container fails closed.

## Tests and current proof

Fleet current Phase 2 branch:

- full Python suite: **767 passed**;
- Phase 2 OCI unit + real-Docker focused suite: **36 passed**;
- real Docker workshop proof covers hardened posture and zero container residue;
- Ruff: PASS;
- `git diff --check`: PASS;
- public repository hygiene: PASS.

Hermes Agent branch `vnext/phase2-disposable-oci-body`, commit `ef20e27ee`, based on the preserved Phase 1 reconciliation branch:

- preserved Phase 1 API-run + Phase 2 Docker regression slice: **169 passed, 2 skipped**;
- Phase 2 exact-workshop verifier + real Docker entry proof: **20 passed**;
- real Docker proof executes inside the exact non-root workshop and proves Hermes cleanup leaves the Fleet-owned container running;
- Ruff: PASS;
- `git diff --check`: PASS.

The two skipped Agent tests are pre-existing conditional cases in the preserved API-run suite, not Phase 2 Docker failures.

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
