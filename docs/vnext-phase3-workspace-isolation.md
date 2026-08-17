# Hermes Fleet vNext Phase 3 acceptance: workspace and filesystem isolation

Status: **COMPLETE**

Phase 3 makes project filesystem access an explicit, bounded authority surface while preserving the Phase 2 rule that the OCI workshop is a disposable execution body only. Host project trees are never mounted into the workshop by default. Fleet copies only already-authorized inputs into per-run tmpfs, exports only declared outputs, and destroys the disposable filesystem only after exact Hermes finalization evidence proves quiescence.

The master plan introduces the full immutable `RunAuthority` object later in Phase 10. Phase 3 therefore implements only the filesystem enforcement seam: `FilesystemAuthorityScope` consumes one already-verified RunAuthority hash plus explicitly approved, separate write-authority hashes. It does not mint, widen, or infer authority.

## Canonical integration

Fleet Phase 3 merged through PR #135 after required CI passed. Canonical Fleet merge commit: `9e4cb3ce67cdb8f5702c6fc14a00806d6e87bcfd`.

Hermes Agent Phase 3 merged through PR #3 after required CI passed. Canonical Agent merge commit: `2a6ff3404c256ba1bfc8af5c9d3e09fef063fb40`.

The exact source trees exercised by the cross-repository Docker proof match the merged trees:

- Fleet tested tree and merge tree: `007efa07ea80ed8436f015fac4dde3ed67d0194e`;
- Agent tested tree and merge tree: `71dc31faa28c022a41ac3d9fff0165a572896e63`.

Historical Phase 3 branches and older acceptance notes are evidence only. Canonical owner-repository `main` plus green CI are the closure authority.

## Default disposable filesystem posture

| Requirement | Canonical behavior | Result |
| --- | --- | --- |
| Per-run writable workspace | Fleet workshop provides bounded writable tmpfs `/workspace`. | PASS |
| tmpfs `/workspace` | Fleet creates and verifies bounded `/workspace`; Hermes independently verifies it is bounded, writable, `nosuid`, `nodev`, and owned by the Agent UID/GID. | PASS |
| tmpfs `/tmp` | Fleet creates and verifies bounded `/tmp`; Hermes independently verifies positive bounded size, Agent ownership, mode `0700`, and required tmpfs flags. | PASS |
| Bounded disposable home | Fleet creates `/home/fleet` as bounded tmpfs and injects `HOME=/home/fleet`; Hermes independently verifies the same bounded ownership/mode posture. | PASS |
| No host `$HOME` | No bind/volume projection is used; broad host home roots are rejected. | PASS |
| No automatic host cwd mount | Host cwd is never inferred or mounted. Project access begins from configured project identity plus an explicit filesystem grant. | PASS |
| No bind mounts by default | Project data uses bounded copy-in/copy-out through the existing Fleet-owned container; observed bind/volume mounts fail closed. | PASS |

Fleet verifies exact configured tmpfs limits for all four zones. Hermes verifies the security property independently without duplicating Fleet's allocation policy: each zone must have exactly one positive `size=` value no larger than its Phase 3 maximum, plus the expected UID/GID, mode, and `rw,nosuid,nodev,exec` posture.

## Filesystem authority model

A `FilesystemGrant` contains only:

- trusted destination `project_id`;
- relative project path;
- in-container target beneath `/workspace`;
- `read` or `write` mode;
- byte limit;
- exact verified RunAuthority hash reference;
- for write mode only, a separate write-authority hash.

Raw host absolute paths are not runtime grants.

`FilesystemAuthorityScope` binds every grant to one exact `sha256:` RunAuthority hash. Read access is the default. A writable grant is accepted only when its distinct write-authority hash is explicitly present in the verified scope; the write-authority hash may not equal the RunAuthority hash.

Fleet bounds the number of grants, per-grant bytes, aggregate staged bytes, and in-container targets. Exact duplicate **and nested/overlapping** targets fail closed so one projection cannot shadow or overwrite another authorized projection.

## Canonicalization and host-state defense

Authorization order is intentionally fixed:

1. resolve the configured project root;
2. form the candidate beneath that root;
3. canonicalize the candidate strictly;
4. prove it remains within the configured root;
5. reject forbidden host state, sensitive components, symlinks, special files, and hard-linked files;
6. evaluate the verified filesystem authority scope;
7. measure the bounded tree;
8. build and stage a deterministic no-link archive.

The resolver rejects traversal, absolute runtime source paths, symlink project roots, symlink escapes, special entries, hard-linked files, and sensitive-state components anywhere in the selected tree. A broad project selection cannot smuggle a nested sensitive directory into the workshop.

Forbidden host-state policy covers the master-plan bans, including system roots/home roots, Docker control state, SSH state, Fleet state, Keryx state, Nodescale state, and configured Vault backing state. Additional destination-specific forbidden paths may be supplied by the trusted Fleet configuration.

The staging pass revalidates file type, hard-link count, and sensitive components while reading, and regular files are opened with no-follow semantics where supported. This prevents the earlier authorization result from becoming a license to follow a path that changed before archive creation.

## Read-only and writable projections

Read projections are staged beneath `/workspace/inputs` using a distinct non-root staging identity (`65533:65533`). Their archive modes are stripped of write bits. Hermes runs as `65532:65532`, so it cannot write the staged files or chmod them writable again.

Writable project grants are staged beneath `/workspace/work` as Agent-owned disposable copies and require separate write authority. They are **not** writable host bind mounts. A real-Docker proof mutates the writable copy and confirms the original host project file remains unchanged.

This is deliberately stricter than exposing an authorized host project through a writable bind mount. Host mutation belongs to explicit artifact/export or later broker-controlled effects, not ordinary workspace access.

## Artifact export

`ArtifactExportGrant` permits only declared outputs beneath `/workspace/out` and bounds each export plus the aggregate export size.

Fleet exports each declared path as a tar stream and validates it before returning bytes:

- no absolute or traversal member paths;
- no links or special entries;
- no duplicate normalized member paths;
- every member must remain under the exact declared export root;
- the declared root must be present;
- file bytes remain within the grant limit.

Output scanning is optional unless the grant requires it. Whenever a scanner is invoked, it must return exactly `True`; `False`, `None`, exceptions, or any other indeterminate response fail closed. A required scan with no scanner also fails closed.

Undeclared workspace files are not exported.

## Quiescence-gated destruction

Phase 3 now closes the master-plan requirement that the run filesystem is destroyed **after quiescence** rather than deferring that requirement to Phase 8.

`WorkspaceQuiescenceProof` consumes exact Phase 1 Hermes finalization evidence and requires:

- exact expected Hermes run ID;
- terminal status `completed`, `failed`, or `cancelled`;
- `quiescent=true`.

`destroy_workspace_after_quiescence(...)` additionally requires the exact Fleet `ExecutionPlan` and `BackendExecutionHandle` to agree on execution ID and plan fingerprint. Only after those checks pass may it invoke the existing Phase 2 Fleet cleanup lifecycle. Invalid or non-quiescent evidence leaves the workshop intact.

Phase 8 may later orchestrate this seam as part of the full Run Capsule lifecycle, but Phase 3 itself no longer relies on a later phase to establish the filesystem-destruction ordering guarantee.

## N → N+1 and cross-repository Docker proof

The real-Docker Phase 3 proof establishes the full temporary-filesystem boundary:

1. Fleet creates an exact hardened workshop.
2. Fleet resolves and stages an authorized read projection.
3. Hermes independently verifies the Phase 2+3 Docker posture and attaches to that exact container.
4. Hermes reads the projected input but cannot append to it or chmod it writable.
5. Hermes releases its environment without stopping or deleting the Fleet-owned container.
6. Non-quiescent finalization evidence is rejected and the container remains running.
7. Exact terminal `quiescent=true` evidence permits Fleet cleanup.
8. Hermes cannot reattach to the destroyed exact container.
9. Fleet creates run N+1 with a different container.
10. Run N+1 cannot see run N input, writable-copy, output, or other temporary workspace state.
11. The writable projection used during run N did not mutate the original host project file.
12. The proof leaves zero Fleet workshop container residue.

Cross-repository proof marker: `PHASE3_CROSS_REPO_DOCKER_PROOF_OK`.

## Validation record

### Fleet

- complete local Fleet suite: **864 passed**;
- focused Phase 2+3 OCI/workspace suite: **65 passed**;
- Ruff: PASS;
- `git diff --check`: PASS;
- public repository hygiene: PASS;
- Fleet PR #135 required CI: PASS;
- resulting Fleet `main` CI run `32043893178`: PASS across Python 3.11, Python 3.13, Rust workspace compatibility, real Nodescale/readiness proofs, and Hermes clean-install smoke.

The first PR #135 CI run exposed only Ruff formatting differences in two files. A normal follow-up commit corrected those mechanical differences; the subsequent required CI and post-merge `main` CI passed.

### Hermes Agent

- Phase 3 exact-workshop verifier + real Docker: **33 passed**;
- preserved Phase 1–3 regression slice: **135 passed, 2 platform-specific skips**;
- Ruff on affected files: PASS;
- `git diff --check`: PASS;
- Agent PR #3 CI: PASS after infrastructure-only reruns;
- resulting Agent `main` CI run `32043471391`: PASS;
- resulting Agent Docker build/test workflow `32043470744`: PASS.

The first Agent CI attempt was disrupted by GitHub-hosted action/runner failures, including HTTP rate-limit/download failures before tests ran. After the required `ci-reviewed` maintainer gate was satisfied and failed infrastructure jobs were rerun, all required code/test/security lanes passed without a Phase 3 code workaround.

## Phase 3 closure

Phase 3 is closed only because all of the following are simultaneously true:

1. Phase 2 remains canonical and green.
2. Fleet filesystem authority/canonicalization/staging/export code is on canonical Fleet `main`.
3. Hermes Phase 3 independent workspace verification is on canonical Agent `main`.
4. Both code changes merged through green PR gates and both resulting `main` CI runs are green.
5. The tested source trees exactly match the merged trees.
6. Real Docker proves immutable read staging, separately authorized disposable write copies, declared/scanned export, quiescence-gated destruction, and N+1 zero residue.
7. No Phase 4+ network implementation was used to satisfy Phase 3.

Phase 4 network-isolation work already present in the repository remains evidence only until Phase 4 is separately re-audited and canonically closed.
