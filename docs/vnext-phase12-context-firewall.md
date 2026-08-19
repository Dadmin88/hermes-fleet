# Hermes Fleet vNext Phase 12 acceptance: context firewall

Status: **CLOSURE GATED**

Phase 12 is complete only after this implementation is merged with green PR CI and the resulting `main` commit also has green CI.

## Boundary

Phase 12 adds a pre-prompt authorization firewall around Hermes native memory and skill retrieval. Fleet remains the deterministic authority. Model output, memory, skills, and the firewall itself cannot widen RunAuthority.

The Phase 12 path is:

```text
current PrincipalReference
+ current RunAuthority / Run Capsule
+ verified persistent Agent Instance
+ immutable Agency base manifest
        |
        v
Fleet fleet-context-v1 binding
        |
        v
Hermes ContextVar-scoped firewall
        |
        +-- authorize/filter memory candidates
        +-- classify/verify skill provenance
        +-- sanitize shared/lower-trust content
        +-- attach provenance and authority=none
        +-- enforce context bounds
        v
model prompt / skill result
```

## Fleet binding

Fleet derives `fleet-context-v1` only after revalidating the exact principal and RunAuthority immediately before Hermes submission.

It binds:

- principal ID, kind, generation, and binding hash;
- persistent Agent Instance ID;
- exact immutable Agency-base manifest digest;
- exact RunAuthority hash.

The context binding is submitted beside `fleet_runtime` and `fleet_memory`. Hermes must advertise `run_fleet_context_firewall`; otherwise Fleet fails closed and does not silently fall back to unfiltered context.

The context principal and Agent Instance must exactly match the Phase 11 memory binding.

## Memory firewall

For an explicit Phase 12 run, Hermes validates each memory candidate before prompt construction:

- current principal identity;
- authorized read scope;
- owner principal metadata;
- Agent Instance relevance;
- retention;
- revocation;
- sensitivity;
- trust state;
- promotion state;
- content hash;
- provenance metadata.

Principal-private memory remains private. Non-principal scopes remain visible only through the explicit promotion semantics established in Phase 11.

Stored prompt injection or authority-manipulation content is not inserted raw into model context. The firewall substitutes a safe blocked marker with provenance rather than exposing the poisoned text.

## Skill firewall

Phase 12 distinguishes immutable professional base skills from later persisted or externally supplied skill content.

An Agency-base file receives base trust only when:

- the active Fleet context binds the exact base-manifest digest;
- the profile manifest is private and structurally valid;
- the file path resolves inside the bound profile;
- the path has not become a symlink;
- size, mode, and SHA-256 still match the immutable manifest.

This avoids treating legitimate security/tooling documentation as hostile merely because it discusses credentials or privileged operations. The bundled-skill audit found 17 of 82 skills would be false positives under the strict lower-trust scanner; the manifest-bound base policy produces zero such false positives while direct prompt-injection and authority-manipulation payloads remain blocked.

Learned, plugin, external, or otherwise non-manifest skill content stays lower-trust and receives strict scanning. Skill descriptions and skill-index context are sanitized and bounded. Full skill and linked-file results carry provenance, trust class, content identity, and `authority=none` semantics.

Fleet-bound skill preprocessing does not execute inline shell while constructing model context.

## Compatibility

Phase 11-only clients remain compatible when they do not send `fleet_context`. Once a client explicitly opts into `fleet-context-v1`, failures are fail-closed.

Fleet CI pins the exact merged Hermes Agent revision implementing the firewall and performs a serializer compatibility smoke against it.

## Acceptance evidence before PR

Local evidence includes:

- Hermes Agent Phase 12 PR #7 green on exact head `1728e7f43ec59085a0914b3a2ea7a988e195d78e`;
- Hermes Agent merge commit `ea70ede77e2da3d920709a9baf8059d7373df204` with green post-merge main CI;
- 66 focused Agent security/compatibility tests passing;
- full Agent Windows-footgun scan passing across 964 files;
- all 12 Agent Python CI slices green;
- Fleet targeted context/runtime/memory/executor tests passing;
- direct Fleet-to-Hermes `fleet-context-v1` round-trip proof passing;
- 979 broader Fleet tests passing, 2 skipped;
- Fleet Ruff, public-hygiene, Desktop plugin syntax, and whitespace checks passing.

This document remains closure-gated until Fleet PR CI and the resulting Fleet `main` CI are green.
