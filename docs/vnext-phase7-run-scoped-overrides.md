# Hermes Fleet vNext Phase 7 acceptance: run-scoped Hermes execution overrides

Status: **IN PROGRESS — canonical Fleet reconciliation pending**

Phase 7 adds the native Hermes seam for temporary Fleet execution state. The durable Agent Instance remains a normal persistent Hermes profile. The exact disposable body binding exists only for one `/v1/runs` task through a `ContextVar`; it is never written into the profile, process-global environment, or shared terminal configuration.

## Exact `fleet_runtime` contract

Hermes accepts one optional `fleet_runtime` object on `/v1/runs` with exactly these fields:

```text
version: fleet-run-v1
container_id: exact 64-hex Docker ID
plan_fingerprint: exact sha256
image: digest-pinned OCI image
toolsets: [fleet-terminal]
max_iterations: bounded integer
```

The parser rejects:

- unknown/extra fields;
- short, named, or otherwise non-exact container identities;
- invalid plan fingerprints;
- image tags without an OCI digest;
- any toolset other than exactly `fleet-terminal`;
- multiple toolsets;
- boolean/zero/excessive iteration budgets.

Because the object has an exact key set, it has no representation for arbitrary Docker flags, mounts, environment injection, network enablement, host paths, or container persistence.

## ContextVar-only temporary authority

Canonical Hermes Agent fork `Dadmin88/hermes-agent` now carries the implementation on `main` via PR **#5**. PR head `25e07b774f2f84d049faad30d07c56c1840bfd63` merged as `04be624ceb3ebadca0d514f4276a146cdc7296e9`. It defines immutable `FleetRuntimeBinding` in `agent/fleet_runtime_scope.py` and stores it only in a `ContextVar`. No write or PR was made to `NousResearch/hermes-agent`.

`/v1/runs` validates the payload before creating any run state. When valid, it enters the Fleet runtime scope only long enough to create the background asyncio task. Python task-context capture gives that task its own immutable binding, and the request handler immediately restores its previous context.

The run task explicitly copies its context into the executor-thread call that runs the synchronous Agent loop. This matters because the actual tool execution can occur off the asyncio thread.

Focused concurrency proof launches two simultaneous Runs API requests with different container IDs, plan fingerprints, and iteration budgets. It verifies:

- each Agent is created under its own binding;
- each executor thread observes the matching binding;
- the two bindings never cross;
- the request/test caller context is `None` immediately after each 202 response;
- both runs complete independently.

Nested scope tests also prove exact prior-value restoration.

## Run-scoped toolset and iteration budget

At Agent construction Hermes still resolves its normal persistent/global API-server toolset and normal iteration default. If and only if a Fleet runtime ContextVar is present, those temporary execution inputs are narrowed to:

- `enabled_toolsets = ["fleet-terminal"]`;
- `max_iterations = fleet_runtime.max_iterations`.

A focused test constructs an Agent in Fleet scope and then immediately constructs a normal Agent outside it. The Fleet Agent receives only `fleet-terminal` and the requested bounded iteration count; the later normal Agent receives the original configured toolsets/default budget. No durable configuration is changed.

## Exact attach-only terminal environment

`terminal_tool._create_environment()` checks the run-scoped Fleet ContextVar before consulting generic terminal backend configuration. If present, it constructs `FleetWorkshopEnvironment` directly with:

- exact container ID;
- exact plan fingerprint;
- exact digest-pinned expected image;
- run timeout.

It returns before generic Docker/local/SSH/container configuration is interpreted. A regression deliberately supplies a hostile generic container configuration containing volume, environment, privileged, network, and host-user options. None enter the Fleet environment constructor.

The test also snapshots `os.environ` and proves the Fleet runtime selection changes no process environment variables.

`FleetWorkshopEnvironment` independently re-inspects the exact Docker container before execution. Phase 7 adds exact image binding on top of the Phase 2–4 verification posture. The environment still has no create/start/restart/stop/delete fallback.

## Live Docker proof

A real-Docker Phase 7 test creates a hardened Fleet-style `network=none` workshop with:

- exact full container ID;
- exact plan fingerprint;
- digest-pinned image;
- non-root Agent UID;
- read-only root;
- dropped capabilities;
- no-new-privileges;
- bounded resources;
- tmpfs workspace/input/home/tmp;
- Fleet ownership/deadline labels.

The normal terminal configuration passed to `_create_environment()` says `local` and includes a hostile generic Docker option. Inside a `fleet_runtime` ContextVar, Hermes nevertheless selects the exact Fleet workshop and successfully executes a command there.

After `environment.cleanup()`, Docker inspection proves the container is still running. Hermes entered the body but did not acquire Fleet lifecycle ownership.

## Network posture in Phase 7

The Phase 7 payload deliberately contains no network field and therefore cannot grant networking.

The direct run-scoped binding introduced here uses the original offline Fleet workshop expectation. Phase 4’s mediated-network verifier remains available, but later Run Capsule/RunAuthority orchestration must supply its independently authorized network binding through the appropriate higher-level authority seam. `fleet_runtime` itself will not become a network-grant channel.

This preserves the master-plan rule: temporary run attachment may select an already-authorized body, but cannot broaden its network authority.

## Hermes capability advertisement

Hermes `/v1/capabilities` now advertises:

```text
run_fleet_runtime: true
```

The capability regression exercises the actual endpoint and verifies the feature is present.

## Fleet client capability gate

Fleet adds `HermesFleetRuntimeBinding`, mirroring the same exact six-field contract.

`HermesRunsClient.health()` now reports `run_fleet_runtime` as an explicit feature with a fail-closed default of `False`.

When `start(..., fleet_runtime=...)` is requested, Fleet first fetches Hermes health/capabilities. If Hermes does not advertise `run_fleet_runtime=true`, Fleet raises before posting `/v1/runs`.

The regression server proves the request sequence for an unsupported Hermes instance is only:

1. `GET /health`;
2. `GET /v1/capabilities`;
3. no run POST.

When supported, Fleet posts exactly:

- input;
- the canonical six-field `fleet_runtime` document.

No extra runtime authority is serialized.

## Persistent profile remains untouched

Phase 7 does not write to the Hermes profile.

The implementation contains no config rewrite or process-environment bridge. The temporary binding affects only:

- ContextVar task state;
- transient Agent constructor arguments;
- transient terminal environment selection.

Phase 6 already stores and validates a digest of persistent Agent `config.yaml`, so any later attempt to smuggle run state into the durable profile will fail the Agent Instance integrity checks. The Phase 7 tests separately prove normal toolset/iteration behavior is restored outside Fleet scope and that process environment state is unchanged.

## Current proof

Hermes Agent Phase 7 proof on the canonical fork:

- refreshed implementation branch based on `Dadmin88/hermes-agent:main` only;
- focused ContextVar/API/terminal/live-Docker suite: **10 passed**;
- touched gateway/terminal regression slice: **24 passed**;
- Ruff on all changed files: PASS;
- `git diff --check`: PASS;
- PR **Dadmin88/hermes-agent#5** exact head `25e07b774f2f84d049faad30d07c56c1840bfd63`: all required CI checks PASS, including all 12 Python slices, e2e, macOS/Windows, Ruff/type checks, supply-chain checks, and unrelated-history guard;
- PR #5 merged normally as `04be624ceb3ebadca0d514f4276a146cdc7296e9`;
- resulting fork-`main` CI run `32092599764`: PASS on exact merge SHA;
- resulting fork-`main` Docker Build, Test, and Publish run `32092598601`: PASS on exact merge SHA.

Fleet Phase 7 reconciliation proof before PR:

- Hermes Runs client unit suite: **27 passed**;
- full Fleet Python suite with the verified Hermes environment first on `PATH`: **900 passed, 1 skipped**;
- installed `FleetRuntimeBinding` parser/ContextVar seam probe: `PHASE7_FLEET_RUNTIME_SEAM_OK`;
- clean-install CI is updated to fetch the exact canonical fork merge SHA and execute the runtime-seam probe before the complete Fleet suite.

The remaining Phase 7 closure gate is the Fleet reconciliation PR and resulting Fleet `main` CI on its exact merge head.

## Explicit non-goals retained for later phases

Phase 7 does not yet implement:

- complete Run Capsule lifecycle, cleanup/recovery, and body orchestration: Phase 8;
- formal principal identity: Phase 9;
- full immutable RunAuthority issuance/validation: Phase 10;
- scoped memory/skill retrieval: later phases;
- final removal of the legacy disposable-profile execution path: Phase 37.

The persistent brain and temporary native Hermes execution seam are now separate and ready for Run Capsule orchestration.
