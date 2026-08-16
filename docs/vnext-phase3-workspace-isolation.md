# Hermes Fleet vNext Phase 3 acceptance: workspace and filesystem isolation

Status: **COMPLETE**

Phase 3 makes filesystem access an explicit, bounded authority surface while preserving the Phase 2 rule that the OCI workshop is disposable execution body only. Project data is not exposed by default. Fleet projects already-authorized content into per-run tmpfs and exports only declared outputs.

This phase deliberately does not implement the full Phase 10 `RunAuthority` object early. `FilesystemAuthorityScope` is the Phase 3 enforcement adapter that consumes a verified RunAuthority hash and an explicit set of separately approved write-authority hashes. Phase 10 will become the producer of that verified scope.

## Default filesystem posture

| Requirement | Evidence | Result |
| --- | --- | --- |
| Per-run writable workspace | Phase 2 workshop provides bounded tmpfs `/workspace`; Phase 3 divides it into explicit input/work/output zones. | PASS |
| Initial `/workspace` on tmpfs | `DockerWorkshopBackend` creates and verifies `/workspace` tmpfs. | PASS |
| `/tmp` on tmpfs | Workshop creates and verifies bounded `/tmp` tmpfs. | PASS |
| Bounded disposable home | Workshop uses bounded `/home/fleet` tmpfs and injects only `HOME=/home/fleet`. | PASS |
| No host `$HOME` | No bind/volume projection is used; broad user home is forbidden as a configured project root. | PASS |
| No automatic host cwd mount | No host cwd is inferred or mounted. Project access requires configured project ID plus explicit grant. | PASS |
| No bind mounts by default | Workspace realization uses copy-in/copy-out over `docker exec`; no host bind/volume mount is introduced. | PASS |

## Project authority model

A filesystem grant contains only:

- trusted destination `project_id`;
- relative project path;
- in-container `/workspace` target;
- read/write mode;
- byte limit;
- exact verified RunAuthority hash;
- for write mode only, a separate write-authority hash.

Raw host absolute paths are never accepted as runtime filesystem grants.

`FilesystemAuthorityScope` binds grants to one exact `sha256:` RunAuthority hash. Writable grants are permitted only when their separate write-authority hash appears in the scope's explicit write-authority set. A write-authority hash may not equal the RunAuthority hash.

| Requirement | Evidence | Result |
| --- | --- | --- |
| Read-only inputs by default | `FilesystemGrant.mode` defaults to `read`; read grants may not carry write authority. | PASS |
| Explicit project access only through authority | Resolver requires `FilesystemAuthorityScope`; grants outside the verified RunAuthority hash fail closed. | PASS |
| Writable project access requires separate authority | `mode=write` requires a distinct write-authority hash explicitly permitted by the scope. | PASS |
| Bound grant count | Resolver permits at most eight filesystem projections. | PASS |
| Bound input size | Per-grant and aggregate input byte limits are enforced before staging. | PASS |
| Unique in-container targets | Duplicate targets fail closed. | PASS |

## Canonicalization and host-path defense

Fleet resolves a grant using trusted destination project configuration, not prompt-supplied host paths.

The order is deliberate:

1. form the candidate beneath the configured project root;
2. resolve/canonicalize the candidate strictly;
3. prove the canonical source remains inside the configured root;
4. reject forbidden/sensitive host state and symlink/special-file trees;
5. only then evaluate the verified authority scope;
6. measure and stage the bounded source.

A regression test combines an unauthorized authority hash with a symlink escape and proves the canonical path escape is rejected first. This locks the master-plan invariant that paths are canonicalized before authorization.

Fleet rejects:

- `..` traversal and absolute source paths;
- symlinked project roots;
- symlink escapes from the project root;
- symlinks or special entries inside projected trees;
- broad roots `/`, `/home`, the current whole user home, `/root`, `/etc`, `/proc`, `/sys`, `/dev`, `/run`, and Docker backing state;
- Docker sockets;
- Fleet, Keryx, Nodescale, and Vault backing/state roots, including common per-user state locations;
- credential/state components such as `.ssh`, `.docker`, `.gnupg`, `.aws`, `.kube`, `.hermes`, `.keryx`, `.nodescale`, and `.vault`.

An exact configured project directory nested below a user's home remains valid. The protection is against broad home exposure and sensitive state, not against legitimate narrowly configured projects.

## Read-only versus writable projections

Phase 3 uses disposable copy projection rather than a live host mount.

### Read projection

Read grants target only `/workspace/inputs/...`.

The workshop has a dedicated `/workspace/inputs` tmpfs owned by non-root staging UID/GID `65533:65533`. Fleet stages the archive using `docker exec --user 65533:65533`, with directories/files mode-stripped to read/execute only. Hermes runs as separate non-root UID/GID `65532:65532`.

This is stronger than merely `chmod a-w` on Agent-owned files. The real-Docker proof verifies an input file is `65533:65533:444`, then proves Agent UID 65532 can neither append to it nor `chmod u+w` to restore write access.

### Writable projection

Write grants target only `/workspace/work/...` and are staged as Agent UID/GID `65532:65532`. They require the separately authorized write hash. The Agent can edit the disposable copy, but the host project is not a writable bind mount, so those edits do not mutate host project state directly.

This keeps host mutation out of the filesystem projection path. Later host-action/export policy remains the explicit boundary for effects that leave the disposable body.

## Artifact export

Artifacts are copy-out, not host-mounted output directories.

`ArtifactExportGrant` permits only `/workspace/out` or descendants, with a bounded name and byte ceiling. `DockerWorkspaceIO.export_declared` exports exactly the declared path as a tar stream and validates it before returning bytes to a higher layer.

Validation rejects:

- absolute or traversal paths;
- symlinks, hardlinks, and special entries;
- excessive archive member counts;
- per-export byte overflow;
- aggregate export byte overflow;
- duplicate export names.

An output scanner can be supplied for every export and is mandatory when `scan_required=true`. Scanner errors or rejection fail the export closed. No Phase 3 API silently writes an arbitrary host destination.

## Cross-run residue proof

The real-Docker Phase 3 integration proof runs two separate Fleet workshops.

Run N:

- receives a read projection;
- receives a separately authorized writable projection;
- proves the read projection cannot be written or chmod-restored by the Agent;
- modifies its writable copy;
- creates one declared result plus an undeclared temporary file;
- exports only the declared `/workspace/out` content through a required scanner;
- destroys the Fleet-owned workshop.

Run N+1 is created fresh and proves that Run N's input projection, writable projection, output tree, and undeclared temporary file are all absent.

Phase 3 therefore proves the filesystem body is destroyable and does not leak temporary files between runs. Phase 8 owns the higher-level lifecycle ordering that Hermes finalization/quiescence must complete before Fleet performs this destruction.

## Independent Hermes verification

Hermes Agent branch `vnext/phase3-workspace-isolation`, commit `5e8da6a40`, extends the Phase 2 independent workshop verifier.

Before attachment and command execution Hermes now additionally requires:

- exact Agent container user `65532:65532`;
- `/workspace` tmpfs owned by `65532:65532` with mode `0711`;
- `/workspace/inputs` tmpfs owned by distinct non-root staging identity `65533:65533` with mode `0755`.

Hermes still has no create/start/stop/remove or alternate-container fallback in this path.

## Tests and current proof

Fleet Phase 3 branch current proof:

- full Python suite: **782 passed**;
- focused workspace unit + real-Docker proof: **14 passed** after authority-scope/canonicalization refinements;
- combined Phase 2 + Phase 3 OCI/workspace proof: **50 passed** before the final authority-only refinement, with the later workspace slice green afterward;
- Ruff: PASS on Phase 3 code/tests; full Ruff is re-run at closure;
- real Docker proves immutable read projection, writable disposable copy, declared-only scanned export, cleanup, and N+1 zero residue.

Hermes Agent Phase 3 proof:

- Docker/workshop verifier suite: **72 passed**;
- preserved Phase 1 API-run + Phase 2/3 Docker regression slice: **170 passed, 2 skipped**;
- Ruff: PASS;
- `git diff --check`: PASS;
- branch pushed to `Dadmin88/hermes-agent` as `vnext/phase3-workspace-isolation`.

The two skipped Agent tests are pre-existing conditional API-run tests and are not Phase 3 filesystem failures.

## Explicit non-goals retained for later phases

Phase 3 does not implement:

- controlled network egress: Phase 4;
- host-action broker: Phase 5;
- persistent Agent Instance orchestration: Phase 6;
- run-scoped Hermes `fleet_runtime`: Phase 7;
- finalization/quiescence → destruction orchestration: Phase 8;
- full principal identity and immutable RunAuthority issuance/verification: Phases 9–10;
- Templar output/security gates: later numbered phases.

The filesystem enforcement surface is ready for those later authorities without granting them early.
