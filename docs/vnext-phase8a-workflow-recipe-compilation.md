# Hermes Fleet vNext Phase 8A acceptance: Workflow → Recipe compilation and first-run discovery

Status: **IN PROGRESS — implementation pending PR/main CI**

Phase 8A connects Fleet's existing durable Workflow Library/Canvas to the vNext
Recipe and disposable-body architecture without turning editor state into
authority. A backend-owned immutable Workflow revision is orchestration input.
The compiler deterministically derives one Candidate Recipe for every compile-only
`recipe-step`, then resolution/discovery must close all mandatory unknowns before
an exact Resolved Workflow Recipe can exist.

The phase does **not** issue RunAuthority, schedule work, or start Hermes Agent
runs. `metadata.executionAvailable` remains `false` throughout this phase.

## Workflow truth boundary

The Rust Workflow domain accepts two schemas:

- `fleet.workflow-editor.v1`: historical editor documents; every node remains
  `runtime: unavailable`;
- `fleet.workflow-editor.v2`: compile-capable documents; only the built-in
  `recipe-step` block may carry `runtime: recipe`; all other nodes must remain
  `runtime: unavailable`.

The Recipe runtime marker means **compile this node into a Recipe**. It does not
mean the node may execute, grant permissions, select a machine, open a network,
mount a path, use a secret, or perform a host action.

Fleet state remains the durable truth owner. Workflow v2 revisions are validated
by the Rust domain, stored as immutable numbered revisions in Fleet SQLite, and
returned by the authenticated control API with the exact content hash and
`executionAvailable=false`.

The Python compiler consumes an exact backend revision through
`read_workflow_version()`. It independently canonicalizes the document and
recomputes the raw backend SHA-256 before accepting it. The backend's raw
64-hex hash is normalized at the compiler boundary to `sha256:<hex>` so later
Run Capsule bindings use one canonical representation.

Old v1 documents remain readable. Desktop new drafts use v2. Loading a v1
document produces a v2 in-memory draft only when the operator later saves a new
revision; the historical v1 revision remains immutable.

## Workflow graph compilation

`WorkflowRecipeCompiler`:

1. accepts only exact backend-owned Workflow v2 revisions;
2. rejects malformed envelopes, execution claims, invalid runtime markers,
   duplicate IDs, occupied inputs, dangling/self edges and cycles;
3. topologically orders the graph deterministically;
4. treats only `recipe-step` nodes as executable-intent steps;
5. walks backwards through intermediate non-Recipe graph nodes to derive upstream
   Recipe dependencies;
6. emits one independently content-hashed Candidate Recipe per Recipe Step.

One Workflow may therefore compile to many Recipes. Graph edges establish
orchestration/dependency facts only. They do not grant authority.

## Recipe Step authoring

The Desktop graph editor exposes a built-in **Recipe Step** using the existing
Inspector/configuration-schema system. It can declare requirements for:

- Agency profile logical name/version expression;
- CPU minimum/requested/limit;
- RAM minimum/requested/limit;
- swap policy and PID limit;
- GPU mode, count, vendor, class, minimum VRAM and features;
- operating systems and architectures;
- digest-pinned runtime image and toolchains;
- workspace, `/tmp`, and disposable-home capacities;
- inputs, outputs and artifact names;
- filesystem mode and project-relative requirements;
- network mode, DNS and allowlist requirements;
- Hermes/Fleet toolsets;
- symbolic secret requirements only, never secret bodies;
- structured host-operation requirements only;
- deadline and iteration limits;
- placement capabilities/labels;
- whether discovery is expected for unresolved requirements.

This is the operator-facing equivalent of declaring a Fleet-native,
backend-neutral Compose-style disposable execution environment. It describes
what a step needs. It does not authorize those needs.

## Candidate, Validated and Resolved forms

Phase 8A adds explicit contracts:

- `fleet.candidate-recipe.v1` — may contain unknown or proposed requirements;
- `fleet.validated-recipe.v1` — all mandatory requirements are known and no
  model/execution proposal remains unvalidated;
- `fleet.resolved-workflow-recipe.v1` — binds the validated Recipe to an exact
  resolved Agency identity and exact resolution-validity inputs.

Every Candidate Recipe binds:

- Workflow ID;
- immutable revision number;
- canonical `sha256:<workflow-content-hash>`;
- exact Recipe Step ID;
- compiler version;
- exact derivation-input digest;
- Agency requirement;
- all requirement values;
- per-requirement provenance evidence;
- graph dependencies.

A Resolved Workflow Recipe exposes the exact Phase 8 Run Capsule identity fields:

- logical Recipe hash;
- exact resolved Recipe hash;
- Recipe compiler version;
- requirement-provenance digest;
- Workflow ID/revision/hash/step ID.

## Complete requirement model

The backend-neutral requirement model covers:

- CPU minimum/requested/limit;
- RAM minimum/requested/limit;
- swap policy;
- PID limit;
- GPU required/optional/none, count, vendor, class, minimum VRAM, features;
- platform OS/architecture;
- runtime image and toolchains;
- workspace/tmp/home storage;
- declared I/O/artifacts;
- filesystem posture;
- network/DNS/allowlist requirements;
- toolsets;
- symbolic secret requirements;
- structured host-operation requirements;
- execution limits;
- placement capabilities/labels.

The model deliberately contains requirements, not grants. A Recipe that requests
internet, a secret, a writable project or a host operation cannot create that
permission by declaring it.

## Knowledge states and provenance

Each requirement has one knowledge state:

- `declared` — explicitly authored in the Workflow;
- `derived` — deterministically derived from bounded trusted evidence;
- `discovered` — observed by a constrained probe or separately validated proof;
- `proposed` — model/execution suggestion that remains untrusted;
- `unknown` — Fleet does not yet know.

Unknown is a first-class state. Mandatory `unknown` requirements prevent
validation. Any `proposed` requirement prevents validation until a separate
deterministic proof converts it to discovered/validated evidence. Model output
never becomes authority.

Evidence is content-addressed and records a source kind such as Workflow,
project, Agency, probe, model, execution, policy or cache.

## Deterministic project derivation

`ProjectEvidence` inspects only an allowlisted, bounded set of machine-readable
project files:

- Python manifests/locks;
- Node manifests/locks;
- Cargo manifests/locks;
- Go manifests/sums;
- `project.godot`;
- `Dockerfile`.

Reads are bounded per file and in aggregate, symlinks/special files/hard-link
ambiguity fail closed, and contents are hashed. Arbitrary prose such as README
text is not interpreted as execution requirements or authority.

Known project types can provide conservative CPU/RAM/runtime baselines. Explicit
Workflow requirements override derivation. If neither explicit data nor trusted
evidence can determine a mandatory requirement, it stays `unknown`.

Only a digest-pinned `FROM ...@sha256:...` may become derived runtime-image
evidence from a Dockerfile. Tags are not promoted to exact runtime identity.

## Low-authority disposable discovery probe

When deterministic derivation cannot close required facts, Phase 8A provides a
Fleet-owned discovery workshop. `DiscoveryProbePolicy` requires:

- digest-pinned image;
- low CPU/RAM/PID limits and short deadline;
- `network=none`;
- no host bind mounts in the current probe; any future project staging is constrained to read-only input posture;
- no secret references;
- no host-broker grants;
- no LAN/management-network grant;
- no Docker socket;
- no persistent Agent authority;
- non-root execution;
- all Linux capabilities dropped;
- `no-new-privileges`.

`DockerRecipeDiscoveryProbe` reuses the hardened `DockerWorkshopBackend`. It
binds the body to the exact Candidate Recipe and exact resolved Agency identity
for audit/idempotency, but it starts **no Hermes Agent run**. Probe commands are
direct bounded argv only, run as UID/GID `65532:65532` with no environment or
shell injection surface. Output is bounded and hashed. The workshop is destroyed
in `finally`, and cleanup must prove absence.

A real Docker acceptance test independently inspects the live probe and verifies
read-only root, `network=none`, no bind mounts, `CapDrop=ALL`,
`no-new-privileges`, UID/GID 65532, exact memory/PID limits and absence of
Fleet/Keryx/Nodescale/SSH environment material. It then proves the container is
gone after the observation.

Probe observations may fill only requirements that were `unknown`. Discovery
cannot overwrite a known declaration/derivation.

## Adaptive revision, never live authority widening

Execution observations such as:

- OOM/resource saturation;
- disk exhaustion;
- network denial;
- missing accelerator;
- missing runtime/toolchain;

may produce a new Candidate Recipe with a `proposed` requirement. The current
Recipe is immutable and unchanged. The proposal cannot become Validated until a
separate deterministic validation proof exists, and it cannot widen any live
RunAuthority.

## Exact cache/reuse validity

Successful historical resolution can be cached only under exact validity inputs:

- Workflow content hash;
- project fingerprint;
- Agency fingerprint;
- runtime fingerprint;
- policy fingerprint;
- capability/inventory fingerprint;
- compiler version.

Any changed input is a cache miss. Historical success never overrides current
policy, capabilities or security posture.

## Placement including GPU

Phase 8A adds deterministic placement capability matching for Recipe requirements,
including:

- OS/architecture;
- available CPU/RAM/PID capacity;
- workspace/tmp/home capacity;
- toolchains;
- placement capabilities/labels;
- GPU count/vendor/class/VRAM/features.

A required GPU constraint cannot be satisfied by a destination that merely has
"some GPU". Count, vendor/class when specified, minimum VRAM and required
features must all match.

## Current local proof

Before PR/CI closure, current local evidence includes:

- Candidate/Validated/Resolved requirement-contract tests;
- deterministic Workflow compiler, cycle/identity/provenance and first-run
  discovery tests;
- exact cache invalidation and adaptive-proposal tests;
- GPU placement tests;
- Desktop v2/Recipe-Step plus legacy-v1 compatibility tests;
- disposable discovery-probe unit tests;
- real Docker low-authority discovery-probe proof.

Current local reconciliation proof before PR:

- focused Phase 8A Recipe/compiler/Desktop/discovery slice: **76 passed**;
- full Fleet Python suite: **933 passed, 1 skipped**;
- real Docker low-authority discovery probe: PASS within the focused slice;
- full Ruff: PASS;
- `git diff --check`: PASS;
- direct `rustfmt --check` on all changed Rust files: PASS;
- public-hygiene scan: PASS.

Rust domain, state and control v2 tests are added, but local Cargo execution is
intentionally blocked by the repository's external-build guard because
`/media/kyle/External SSD` is not mounted. That guard is not bypassed; GitHub CI
is the required Rust compile/test proof for this phase unless the external build
volume becomes available.

## Closure gates

Phase 8A is complete only after:

1. full Fleet Python tests pass;
2. full Ruff and `git diff --check` pass;
3. public-hygiene scan passes;
4. Desktop Workflow/Recipe tests pass;
5. Rust Workflow domain/state/control compatibility passes in CI;
6. exact PR-head CI is green, including Hermes clean-install smoke;
7. the PR merges normally;
8. resulting Fleet `main` push CI is green on the exact merge commit.

Phase 9 remains locked until those gates close.
