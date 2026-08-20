# vNext Phase 17: Skill verification

Status: **CLOSURE GATED**

Phase 17 verifies exact Phase 16 `verification-ready` learned-skill candidates inside a disposable runtime before any later promotion decision. Verification adds evidence; it does not activate a skill, widen scope, mutate RunAuthority, or grant authority.

## Ownership boundary

### Hermes Agent owns

- accepting only candidates with a valid immutable Phase 16 quarantine seal and exact content hash;
- requiring the exact original Fleet skill-learning binding and capability manifest;
- static analysis through the existing Hermes skill-guard surface;
- persisted-sensitive-material scanning through the Phase 13 classifier;
- positive and negative verification probes inside a disposable runtime;
- network, management-network, host-filesystem, protected-environment, broker, Docker-socket, and privilege-denial probes;
- CPU, address-space, file-size, file-descriptor, process-count, and wall-clock bounds;
- deterministic verification evidence bound to the candidate content hash, quarantine digest, capability manifest, runtime policy, and check results;
- invalidating prior verification when candidate content or its Phase 16 seal changes;
- keeping both successful and failed verification candidates inactive and authority-free;
- advertising `run_fleet_skill_verification` only when this behavior is available.

### Fleet owns

- refusing a Fleet learning run unless Hermes advertises `run_fleet_skill_learning`, `run_fleet_skill_quarantine`, and `run_fleet_skill_verification`;
- continuing to derive the exact principal/run/RunAuthority/capability envelope that verification must match;
- pinning clean-install CI to the exact merged Hermes Agent revision that implements Phase 17;
- exercising the exact merged-Agent Phase 17 verification seam in CI.

Neither side implements Phase 18 promotion in this phase.

## Verification prerequisites

A candidate may enter Phase 17 only when all of the following are true:

- Phase 16 classified it as `verification-ready`;
- its Phase 16 quarantine record is supported and immutable;
- its current bundle content hash exactly matches the quarantined content hash;
- it remains `active: false` and `authority: none`;
- the supplied Fleet learning binding exactly matches the candidate's principal, Agent Instance, source run, RunAuthority, Recipe, ResolvedRecipe, plan, capabilities, target, tool, filesystem, network, and protected-material envelope.

A `rejected` or `needs-review` candidate does not enter Phase 17 verification.

## Verification runtime

The Agent verifier uses a disposable Linux Bubblewrap sandbox. Candidate-controlled code is not imported or executed by Phase 17. Instead, Hermes performs static/sensitive analysis on the exact bundle and runs fixed verifier-owned positive/denial probes against the disposable runtime itself.

The runtime:

- unshares all supported namespaces, including network;
- runs as UID/GID 65534 with all effective capabilities dropped;
- mounts only the system runtime required by the fixed verifier plus a read-only copy of the candidate bundle;
- does not mount host `/etc`, `/home`, `/root`, `/run`, `/var/run`, `/mnt`, or `/media`;
- exposes no Docker socket or Fleet/Hermes broker socket;
- starts from an empty environment and admits only verifier-owned non-secret variables;
- has no usable internet, private-network, or Tailscale-style management route;
- provides an isolated tmpfs scratch area;
- enforces bounded CPU, address space, output file size, open-file count, process count, and parent wall-clock time.

If the required disposable-runtime primitive is unavailable, verification fails closed and no Phase 17 attestation is persisted.

## Checks and outcomes

Phase 17 records deterministic checks covering:

- static-analysis findings;
- Python syntax validation for candidate Python files without executing them;
- sensitive-material scan;
- exact capability-manifest validation;
- positive bundle-read and isolated-scratch behavior;
- host-filesystem denial;
- inherited-sensitive-environment denial;
- broker and Docker-socket denial;
- internet and management-network denial;
- non-root/effective-capability denial;
- resource-bound enforcement.

All checks passing produces `tests.state: verified`. Any completed check failure produces `tests.state: failed`. Infrastructure inability to establish the verification boundary fails closed without pretending that verification completed.

The Phase 16 candidate state remains `verification-ready` in either case. `verified` is evidence attached under the candidate's `tests` record, not a new authority-bearing candidate state.

## Exact-hash attestation

The verification attestation seals:

- verifier version;
- candidate ID;
- exact candidate content hash;
- exact Phase 16 quarantine digest;
- exact capability-manifest hash;
- verifier runtime-policy digest;
- deterministic ordered check-result digest;
- final verification state.

A matching completed attestation can be reused idempotently. Any content, quarantine, capability-manifest, result, or attestation change invalidates reuse and fails closed.

## Mixed-version safety

Fleet learning submissions require all three Hermes capability bits:

- `run_fleet_skill_learning`
- `run_fleet_skill_quarantine`
- `run_fleet_skill_verification`

This prevents Fleet from creating learned candidates on an Agent that can author and quarantine candidates but cannot perform Phase 17 verification.

## Local acceptance evidence

### Hermes Agent

- Phase 17 implementation branch: `feat/phase17-skill-verification`;
- local relevant regression gate: 82 passed;
- real Bubblewrap denial/bounds integration test: green;
- focused Ruff: green;
- full Ruff: green, aside from an existing malformed-`noqa` warning outside this phase;
- `ty` on the new verifier: zero diagnostics;
- `git diff --check`: green.

Agent acceptance evidence is final:

- implementation PR #13;
- exact PR head: `7f37a2bd5ef5464bba005790443642075be641ae`;
- exact PR-head CI run `32311350577`: completed successfully before merge;
- merge commit: `698c89f711d5b34103e05e1193f9d5fb72212fd8`;
- exact post-merge `main` CI run `32311664501`: completed successfully on the merge SHA.

### Fleet

- Runs capability/compatibility tests: 31 passed locally;
- broad Fleet suite in declared dev environment: 982 passed, 12 environment-only skips;
- Ruff: green;
- public-hygiene scan: green;
- Desktop plugin syntax: green;
- Python package build: green;
- clean wheel install and operational entry-point smoke: green;
- exact merged-Agent Phase 17 seam: `PHASE17_AGENT_VERIFICATION_SEAM_OK` against `698c89f711d5b34103e05e1193f9d5fb72212fd8`;
- Fleet now treats missing `run_fleet_skill_verification` as a hard compatibility failure for skill-learning runs.

Fleet PR-head CI, merge, and post-merge CI evidence remain closure-gated until the Fleet Phase 17 branch passes those gates.

## Later-phase boundary

Phase 17 does **not**:

- promote a skill;
- activate a candidate;
- widen candidate scope;
- make learned behavior global;
- grant tools, filesystem, network, protected-material, broker, or host authority;
- mutate RunAuthority;
- implement Phase 18 or later Templar gates.

`tests.state: verified` therefore means only that the exact quarantined candidate passed the Phase 17 verification contract. It is not permission to execute or install it.
