# Fleet Recipe contracts

Fleet Recipes are the backend-neutral declarative contracts that describe what a
disposable Fleet execution environment requires. They are deliberately closer in
spirit to a Compose-style execution manifest than to Docker CLI flags: Recipes
name requirements; Fleet later decides whether, where and how those requirements
can be satisfied and authorized.

A Workflow is the orchestration/front-end representation. Recipe contracts are
the execution/back-end representation.

```text
backend-owned Workflow revision
        |
        v
Workflow compiler
        |
        +--> Candidate Recipe(s)
        |        |
        |        +--> derivation/discovery/proposal evidence
        |        v
        +--> Validated Recipe(s)
                 |
                 v
        Resolved Workflow Recipe(s)
                 |
                 v
        later admission / RunAuthority / Run Capsule / disposable body
```

A single Workflow may compile to many Recipes. No Recipe grants authority merely
by asking for a capability.

## Compatibility contracts: `fleet.recipe.v1`

The original `FleetRecipe` / `ResolvedRecipe` contracts remain supported because
existing exact-execution and OCI paths depend on them.

`fleet.recipe.v1` carries:

- one logical Agency profile requirement;
- OS/architecture constraints;
- minimum CPU/RAM;
- named isolation/network requirements;
- bounded namespaced extensions.

`fleet.resolved-recipe.v1` binds that logical Recipe hash to an exact immutable
Agency repository/revision/profile/version/content digest.

These contracts are intentionally not reinterpreted as the new Phase 8A format.
They remain compatibility primitives while vNext migration proceeds.

## Phase 8A compile contracts

Phase 8A introduces three explicit forms:

- `fleet.candidate-recipe.v1`;
- `fleet.validated-recipe.v1`;
- `fleet.resolved-workflow-recipe.v1`.

A Candidate Recipe binds the exact Workflow revision/step and may still contain
unknown or proposed requirements. A Validated Recipe has no mandatory unknowns
and no untrusted proposals. A Resolved Workflow Recipe additionally binds the
validated Recipe to an exact Agency identity and exact resolution-validity
fingerprints.

## Complete disposable-environment requirements

The Phase 8A requirement model covers:

- CPU minimum/requested/limit;
- RAM minimum/requested/limit;
- swap policy;
- PID limit;
- GPU none/optional/required, count, vendor, class, minimum VRAM, features;
- operating systems and architectures;
- digest-pinned runtime image and toolchains;
- workspace, temporary-space and disposable-home capacities;
- inputs, outputs and artifacts;
- filesystem requirements;
- network mode, DNS and allowlist requirements;
- Fleet/Hermes toolsets;
- symbolic secret requirements only;
- structured host-operation requirements;
- deadline and iteration bounds;
- placement capabilities and labels.

These are requirements, not permissions. For example:

- `network: internet-approved` means the step requires that capability; it does
  not give the container internet;
- a symbolic secret requirement does not include a secret body or run handle;
- `project-write` does not create filesystem authority;
- a host operation request does not create broker authority;
- GPU requirements do not bypass placement/capability checks.

## Requirement knowledge and provenance

Every requirement is tagged as one of:

- `declared` — explicit Workflow authoring;
- `derived` — deterministic bounded evidence;
- `discovered` — constrained probe/verified evidence;
- `proposed` — untrusted model/execution suggestion;
- `unknown` — Fleet does not know yet.

Unknown is valid Candidate state. A mandatory unknown blocks validation and later
execution authority. Proposed values also block validation until separately
validated. Model output cannot widen authority.

Each known/proposed value carries content-addressed evidence with a source class.
This allows Fleet to answer not only "what does this Recipe require?" but also
"why does Fleet believe it requires that?"

## First-run discovery

When a step has no prior successful Recipe, Fleet follows an evidence ladder:

1. explicit Workflow declarations;
2. deterministic project/Agency/runtime metadata derivation;
3. lower-authority disposable discovery probe when needed;
4. untrusted model/execution proposals only as suggestions requiring validation.

Project derivation reads only bounded allowlisted machine-readable manifests and
lockfiles. Arbitrary prose is not execution authority.

The discovery probe is a normal hardened Fleet workshop with much lower authority
than a run body: no secrets, no broker grants, `network=none`, no management
network, no Docker socket, non-root, all capabilities dropped,
`no-new-privileges`, strict resource/deadline limits and guaranteed cleanup. It
starts no Hermes Agent run.

## Adaptive Recipe revisions

A failed run may supply evidence that the current Recipe was insufficient, such
as OOM, disk exhaustion, missing GPU/runtime, denied network access or resource
saturation. Fleet may produce a new **Candidate** Recipe proposal.

The current Recipe and live RunAuthority remain immutable. The proposal must pass
the same validation/admission process as any other Recipe before a later run can
use it.

## Cache validity

Historical successful resolution is reusable only when all validity inputs match
exactly, including Workflow hash, project fingerprint, Agency/runtime/policy
fingerprints, capability/inventory fingerprint and compiler version. Changed
inputs cause a cache miss. Past success never overrides current policy.

## Canonical identities

Recipe forms use canonical JSON and `sha256:<hex>` content hashes. Workflow state
historically exposes its immutable content hash as raw 64-hex through the Rust
control API; the Phase 8A compiler independently verifies that exact raw value and
normalizes it to `sha256:<hex>` for Recipe/Run Capsule bindings.

A Resolved Workflow Recipe exposes the exact fields needed by the Phase 8 Capsule
identity:

- logical Recipe hash;
- exact resolved Recipe hash;
- compiler version;
- requirement provenance digest;
- Workflow ID/revision/hash/step ID.

The compiler still does not create RunAuthority. That remains later-phase
ownership.
