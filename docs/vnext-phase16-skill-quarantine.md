# vNext Phase 16: Skill quarantine

Status: **COMPLETE**

Phase 16 freezes and deterministically classifies Phase 15 learned-skill candidates before any Phase 17 verification or activation. Candidates remain hidden from normal Hermes skill discovery, inactive, principal-private, and authority-free throughout quarantine.

## Ownership boundary

### Hermes Agent owns

- deterministic quarantine of native Phase 15 candidate bundles after background-review authoring completes;
- reuse of the existing Hermes `skills_guard` threat surface plus Fleet-specific provenance/source-run checks;
- exact content-addressed bundle IDs and tamper-evident quarantine seals;
- immutable post-quarantine candidate state;
- candidate states `rejected`, `needs-review`, and `verification-ready`;
- bounded findings with fail-closed overflow;
- advertising `run_fleet_skill_quarantine` only when this behavior is available.

### Fleet owns

- refusing a Fleet learning run unless Hermes advertises both `run_fleet_skill_learning` and `run_fleet_skill_quarantine`;
- continuing to derive the exact Phase 15 principal/run/authority/tool/filesystem/network/protected-material envelope;
- pinning CI to the exact merged Agent revision that implements Phase 16.

Neither side activates or promotes a candidate in Phase 16.

## Quarantine inputs

The classifier consumes the exact Phase 15 candidate metadata and candidate bundle. It checks:

- principal-private scope and principal generation/binding;
- Agent Instance, source run and RunAuthority;
- Recipe, ResolvedRecipe, plan, capability and target provenance;
- exact source-run tool, filesystem, network and protected-material need envelopes;
- Phase 15 file manifest and content hash;
- sensitive persisted material through the Phase 13 classifier;
- existing Hermes `skills_guard` threat patterns and structural limits;
- dangerous/privileged commands;
- undeclared or unknown tool declarations/calls;
- network use inconsistent with source-run posture;
- protected host paths;
- authority-manipulation language/patterns;
- bounded-finding overflow.

## Deterministic outcomes

- Any critical reason => `rejected`.
- Any high reason and no critical reason => `needs-review`.
- Otherwise => `verification-ready`.

The reason list is canonicalized and sorted before hashing. Quarantine persists a scanner version, exact content-addressed `bundle_id`, content hash, reason digest, quarantine digest, and `immutable: true`.

If a classified candidate's content or quarantine metadata changes later, reuse fails closed. Phase 15 candidate mutation is also refused after classification.

## Mixed-version safety

Fleet learning submissions require both Hermes capability bits:

- `run_fleet_skill_learning`
- `run_fleet_skill_quarantine`

This prevents a newer Fleet from creating learned candidates on an older Agent that cannot freeze/classify them.

## Agent acceptance evidence

Repository: `Dadmin88/hermes-agent`

Implementation PR #12:

- exact PR head: `e23ff8b742d55f0c133fc33bd41575e0f8cc4c02`;
- merge commit: `7cf08061ef4648c60ee91df4c9bcad8857e529a0`;
- exact PR-head CI: green before merge;
- local relevant regression gate: 203 passed;
- full Ruff: green;
- Windows portability scan: 969 Python files, green;
- new quarantine and existing candidate modules: zero `ty` diagnostics;
- modified background-review/gateway diagnostics: exact Phase 15 baseline.

Exact post-merge Agent `main` CI run `32306858940` completed successfully on merge SHA `7cf08061ef4648c60ee91df4c9bcad8857e529a0` with zero failed or pending jobs.

## Fleet local pre-PR evidence

- focused Runs/skill-learning/Run Capsule tests: 55 passed;
- broad Fleet suite: 981 passed, 13 environment-only skips;
- skips are local prerequisites such as pinned Docker images/architecture, QEMU/KVM, or Hermes CLI adjacency;
- Ruff: green;
- formatter: green;
- public-hygiene: green;
- Desktop plugin syntax: green;
- clean Python package build and installed entry-point smoke: green;
- exact merged-Agent quarantine seam: `PHASE16_AGENT_QUARANTINE_SEAM_OK` against `7cf08061ef4648c60ee91df4c9bcad8857e529a0`.

Fleet closure evidence is final:

- Fleet PR #153 exact head: `6e6b927b2880aded5ba2be539fb5e73e7bfe7121`;
- exact PR-head CI run `32308059301`: completed successfully;
- merge commit: `31bb7111e6c4cfd555067224acc460e1a9d95336`;
- exact post-merge `main` CI run `32308293673`: completed successfully on the merge SHA.

Fleet therefore pins and proves the exact closed Agent Phase 16 seam, satisfying the Phase 16 closure rule.

## Later-phase boundary

Phase 16 does **not** implement:

- Phase 17 positive/negative verification or activation eligibility;
- Phase 18 scope promotion;
- Templar gates from later phases.

`verification-ready` therefore means only that deterministic quarantine found no critical/high blocker. It does not make the candidate active or trusted.
