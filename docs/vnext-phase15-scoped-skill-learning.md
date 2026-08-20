# vNext Phase 15: Scoped skill learning

Status: **COMPLETE**

Phase 15 implements the master-plan scoped skill-learning boundary while preserving Hermes native skill format/loading and Fleet's existing authority model. It does not activate autonomously learned skills and does not implement Phase 16 scanning/classification or Phase 17 verification.

## Ownership boundary

### Hermes Agent owns

- native `SKILL.md` candidate content and supporting files;
- the existing `skill_manage` primitive and background-review learning fork;
- the hidden per-profile candidate overlay;
- active-skill discovery exclusion for inactive candidates;
- principal-private candidate filesystem posture;
- candidate bundle content hashes and source-run metadata;
- Phase 13 interception before candidate persistence;
- preserving existing curator ownership/pin/bundled/external-skill guards.

### Fleet owns

- deriving the exact skill-learning envelope from the current principal and immutable RunAuthority-backed Run Capsule;
- principal-private scope selection;
- exact source-run, Agent Instance, Recipe, ResolvedRecipe, plan, capability, and target provenance;
- source-run tool, filesystem, network, and one-way material-reference need metadata;
- requiring Hermes to advertise `run_fleet_skill_learning`;
- exact identity agreement among runtime, memory, context, and skill-learning bindings.

Neither Fleet metadata nor candidate content grants future authority.

## Candidate lifecycle in Phase 15

A Fleet-bound Hermes background review may propose native skill changes through `skill_manage`. Phase 15 redirects those autonomous writes away from the active skill tree and into:

`<profile>/skills/.fleet/candidates/<candidate-id>/`

A candidate contains:

- native `SKILL.md`;
- optional native supporting files;
- `candidate.json` metadata.

The `.fleet` tree is excluded from normal Hermes skill discovery. Candidate directories are private and reject symlink/path redirects. On Unix-like hosts the hidden overlay/candidate directories are `0700` and candidate files are `0600`.

Candidates default to:

- principal-private scope;
- `state = quarantined`;
- `active = false`;
- `authority = none`;
- `risk.state = unassessed` with Phase 16 recorded as the next classifier phase;
- `tests.state = unverified` with Phase 17 recorded as the next verification phase.

Phase 15 does not scan/classify candidate risk and does not verify/activate candidates.

## Exact candidate identity

A candidate ID binds:

- candidate schema;
- principal ID;
- Agent Instance ID;
- source run;
- RunAuthority hash;
- exact plan fingerprint;
- skill name.

When a candidate is reopened, Hermes also verifies the persisted principal generation/binding, scope, Agent Instance, RunAuthority, Recipe, ResolvedRecipe, plan, capability hash, and target digest before any candidate mutation. Candidate metadata tampering therefore fails closed before content changes.

## Candidate metadata

`candidate.json` records the Phase 15 fields required by the master plan:

- principal;
- private scope;
- source run;
- Agent Instance;
- provenance;
- command inventory;
- source-run tools;
- filesystem needs;
- network needs;
- one-way material-reference need fingerprints;
- risk state;
- content hash and file manifest;
- source-run evidence binding;
- test/verification state.

The command inventory is descriptive only. It extracts bounded command names from shell-fenced `SKILL.md` content and supporting shell scripts. Dangerous-command classification remains Phase 16 work.

Filesystem/network/material fields describe the exact source-run envelope. They are not executable grants and cannot modify RunAuthority.

## Existing Hermes ownership behavior preserved

Foreground/user-directed `skill_manage` behavior is unchanged.

The autonomous background curator's existing rules still win before candidate routing:

- pinned skills remain off-limits;
- external skills remain off-limits;
- protected built-ins remain off-limits;
- hub/bundled skills remain off-limits;
- user-owned active skills remain off-limits unless explicitly curator-managed/adopted;
- background edits still require prior read-before-write evidence.

Phase 15 therefore does not use candidate creation as a way to bypass existing Hermes skill ownership policy.

A Fleet-bound background review can create a new private candidate and refine that same candidate during the source run without activating it.

## Run-scoped wire contract

Fleet sends `fleet_skill_learning` version `fleet-skill-learning-v1` alongside the existing runtime/memory/context bindings.

Hermes requires the full Fleet runtime, memory, and context bindings and independently checks:

- principal ID/kind/generation/binding hash;
- Agent Instance ID;
- source run;
- RunAuthority hash;
- plan fingerprint against `fleet_runtime`;
- source-run toolset against `fleet_runtime`.

The binding is ContextVar-scoped to one `/v1/runs` task and is inherited by the native Hermes background-review thread through the existing context propagation mechanism.

## No-authority invariant

A candidate cannot:

- grant itself tools;
- grant itself filesystem access;
- grant itself network access;
- grant itself material access;
- alter approval budget;
- alter host grants;
- modify or widen RunAuthority;
- become active merely because it was persisted.

Candidate need metadata is evidence for later policy/scanning/verification phases only.

## Agent acceptance evidence

Repository: `Dadmin88/hermes-agent`

Implementation PR #11:

- exact PR head: `87ae3c5959cd3a9fcba225e38c156f6580a76cd3`;
- merge commit: `1b6acd0d92ea4e176710825bf446900a66dd7def`;
- full PR matrix: green, including all 12 Python slices, e2e, Windows/macOS, Ruff/type-diff, Windows portability, history, attribution, supply-chain and lock checks;
- local relevant regression gate: 132 passed;
- final focused hardened gate: 41 passed;
- full Ruff: green;
- Windows portability scan: 968 Python files, green;
- new Phase 15 modules: zero `ty` diagnostics;
- modified legacy files: exact Phase 14 type-diagnostic baseline.

Exact post-merge Agent `main` CI run `32243877677` completed successfully on merge SHA `1b6acd0d92ea4e176710825bf446900a66dd7def`, with zero failed or pending jobs.

## Fleet local pre-PR evidence

- targeted skill-learning / Runs / Run Capsule tests: 55 passed;
- broad Fleet suite: 982 passed, 12 skipped;
- all 12 skips are environment prerequisites only: unavailable pinned Docker images/architecture or Hermes CLI absent beside the isolated interpreter;
- full Ruff: green;
- formatter check: 145 files formatted;
- public-hygiene scan: green;
- Desktop plugin syntax: green;
- Python package build: green;
- clean-wheel operational entry points and Phase 15 import smoke: green.

Fleet closure evidence is final:

- Fleet PR #152 exact head: `b2557efdcaaf6a7882d0ca7c11f537e4664ae5c4`;
- exact PR-head CI run `32276601643`: completed successfully;
- merge commit: `331c604e894e10d39bcf109197b8ca38b4ce1639`;
- exact post-merge `main` CI run `32276894660`: completed successfully on the merge SHA.

The Agent dependency and Fleet compatibility gate are therefore both closed for Phase 15.

## Later-phase boundary

Phase 15 does **not** implement:

- Phase 16 dangerous-command/undeclared-tool/network/host-path/authority-manipulation scanning or risk classification;
- Phase 17 positive/negative verification and activation eligibility;
- Phase 18 promotion to project/network/owner scope;
- Phase 24 Agency-base upgrade reconciliation.

Phase 15 creates useful, attributable, private learning candidates. Later phases decide whether any candidate deserves to become active or broader than principal-private.
