# Hermes Fleet project provenance

Hermes Fleet is an open-source control plane for distributed Hermes systems. This document records the public development history of the project’s major architectural milestones so that readers can trace important ideas from proposal through implementation and verification.

This is a provenance record, not a claim of exclusivity over generic distributed-systems techniques. Concepts such as hashing, retries, capability discovery, scheduling, fencing, durable journals, and fail-closed validation have broad prior art. The purpose here is narrower: to make Hermes Fleet’s own architecture, chronology, terminology, and implementation lineage easy to verify from immutable public Git history.

Dates below use the public GitHub record. Commit links are immutable references to the cited state of the repository.

## Architectural through-line

Fleet has consistently separated distinct kinds of authority instead of treating “connected” as “trusted” or “executing” as “successful”:

```text
private connectivity
    != device trust
    != authenticated transport identity
    != Fleet authorization
    != scheduler readiness
    != execution permission
    != verified execution outcome
```

The surrounding repositories keep deliberately separate ownership boundaries:

- **Hermes Nodescale** owns device identity, membership, trust, and binding of stable managed devices to transport identities.
- **Hermes Keryx** owns authenticated inter-machine task/result/artifact transport and its durable transport lifecycle.
- **Hermes Fleet** owns Fleet authorization, exact-node control, scheduling/admission, execution correlation, operator surfaces, and the evolving vNext execution-security model.
- **Hermes Agent** owns local agent execution, models, tools, sessions, memory, skills, and native Runs behavior.
- **Hermes Agency** owns versioned professional profile/capability packages and their immutable source material.

The result is a control plane designed so that transport, placement, execution, evidence, and authority can evolve independently without silently granting one another privileges.

## Public milestone timeline

| Date | Milestone | Public evidence |
| --- | --- | --- |
| **2026-08-08** | Fleet adopts **AGPL-3.0-only** for current versions while preserving the historical MIT notice for earlier published commits. | [`ad51feef`](https://github.com/Dadmin88/hermes-fleet/commit/ad51feef0e75e3a9ea0ce473f9742c453018eadb) |
| **2026-08-09** | Layered managed-node readiness lands with explicit admission/freshness fencing rather than a single magic `ready` bit. | [`8d99267b`](https://github.com/Dadmin88/hermes-fleet/commit/8d99267bd6bbcb4a7a8a62828e21de7deb630385) |
| **2026-08-10** | Fleet exposes deterministic read-only placement-candidate discovery over admitted, ready nodes. | [`1596d4f1`](https://github.com/Dadmin88/hermes-fleet/commit/1596d4f1de302eee8aa4bf5a2a74bda05998c43d) |
| **2026-08-10** | Fleet Canvas begins as a truthful operator graph and durable workflow-authoring surface, explicitly separating visual topology from execution authority. | [`cd6cdd84`](https://github.com/Dadmin88/hermes-fleet/commit/cd6cdd8443efdbd85cea5b5443996f40fded0632) |
| **2026-08-11** | The public architecture moves from persistent host-profile placement toward a **runtime-neutral Fleet Execution Fabric**: Recipe → resolution → execution plan → capability-aware scheduling/local admission → execution backend → Hermes → Keryx result/artifact return → cleanup. | [`3033d204`](https://github.com/Dadmin88/hermes-fleet/commit/3033d204b3b68bdc50f00b15bf68052855c2efcd), [issue #28](https://github.com/Dadmin88/hermes-fleet/issues/28) |
| **2026-08-13** | Versioned **Fleet Recipe** contracts land as runtime-neutral logical requirements with immutable resolution identity. | [`563e733b`](https://github.com/Dadmin88/hermes-fleet/commit/563e733ba799a249277f3148faf6017c5789f922) |
| **2026-08-13** | Provider-neutral **BackendCapabilities** contracts land. Hard eligibility is explicitly separated from ranking, selection, authorization, reservation, and execution. | [`67707943`](https://github.com/Dadmin88/hermes-fleet/commit/677079430fef03cf3e8935f3fc05ea44b38f9819), [`docs/backend-capabilities.md`](backend-capabilities.md) |
| **2026-08-13** | Fleet adds an exact-target execution timeline and public demo kit for the execution-fabric path. | [`7a740b5b`](https://github.com/Dadmin88/hermes-fleet/commit/7a740b5bc9d6ea632e2ea605a4d625cb7e16deef), [`11292f73`](https://github.com/Dadmin88/hermes-fleet/commit/11292f7354fa66d9f833c4047c4be901f7bb03c9) |
| **2026-08-13** | A concrete Docker OCI execution backend lands, keeping the domain model provider-neutral while using a mature runtime for real isolation. | [`cf3bcef3`](https://github.com/Dadmin88/hermes-fleet/commit/cf3bcef36b5938b6493c06aa5f194125d1995a6f) |
| **2026-08-13** | **Durable execution instances** land with idempotency identity, Recipe/capability hashes, exact managed-node identity, generation fencing, explicit indeterminate state, and recovery semantics. | [`090b0923`](https://github.com/Dadmin88/hermes-fleet/commit/090b0923c7960dc0dc8b219b5b742764813acdd6), [`docs/durable-execution-instances.md`](durable-execution-instances.md) |
| **2026-08-13** | Fleet exposes a destination-local execution-control API and begins driving exact Recipes on managed destinations under explicit admission checks. | [`e96cdb7c`](https://github.com/Dadmin88/hermes-fleet/commit/e96cdb7c6d6df8c1319c9fe941b41a8b7c01dddf), [`f8a8a72f`](https://github.com/Dadmin88/hermes-fleet/commit/f8a8a72fc476aae63e60d5601d1fa01446b098fc) |
| **2026-08-14** | Execution cleanup is fenced by execution ownership, preventing a stale or unrelated actor from reclaiming another execution’s realization. | [`0439eb41`](https://github.com/Dadmin88/hermes-fleet/commit/0439eb410cc9f658d10e194d782470f1e8ee7988) |
| **2026-08-16** | Fleet explicitly separates **transport status from execution status**. | [`9368bf61`](https://github.com/Dadmin88/hermes-fleet/commit/9368bf618a28c5f22fc8ba42defd5cbe8509d2d4) |
| **2026-08-16** | Exact execution delivery retries are bound to execution identity rather than blindly minting new work. | [`6e95af03`](https://github.com/Dadmin88/hermes-fleet/commit/6e95af032044b6132344e7b87cae285f4f82533a) |
| **2026-08-16** | Fleet preserves typed execution-outcome evidence and verifies Hermes command/tool outcomes rather than treating a worker’s prose as authoritative success. | [`6f884748`](https://github.com/Dadmin88/hermes-fleet/commit/6f88474872757a29cd28971a0cfe1758507caee0), [`5515199d`](https://github.com/Dadmin88/hermes-fleet/commit/5515199d8920355c414d0a14beb1d482a1c2df87), [`a67ae747`](https://github.com/Dadmin88/hermes-fleet/commit/a67ae7472ba0533093dbf404992f9264f61be6fa) |
| **2026-08-17** | Run Capsule identity/recovery is reconciled so temporary execution state can recover without silently changing logical execution identity. | [`8bd587bd`](https://github.com/Dadmin88/hermes-fleet/commit/8bd587bd1b417b94b2d2ffca6e62cf7f5e1d8b35), [`12bb6856`](https://github.com/Dadmin88/hermes-fleet/commit/12bb685636263534aa79971fc2aa7d1fdacfe1f8) |
| **2026-08-18** | Durable Workflows compile into discovered Recipes, preserving the separation between authoring and exact executable ingredients. | [`7af9f5ee`](https://github.com/Dadmin88/hermes-fleet/commit/7af9f5ee4fbc33e19687affd6dd5f981c2eab685), [`2a5c1577`](https://github.com/Dadmin88/hermes-fleet/commit/2a5c1577eb958ea8462ccdef6524a51569de941d) |
| **2026-08-18** | Immutable **RunAuthority** becomes an explicit vNext authority primitive. | [`5ed35a17`](https://github.com/Dadmin88/hermes-fleet/commit/5ed35a175c34dd5c592e388bf487a41108389ab4) |
| **2026-08-19** | Skill-learning work is constrained by explicit quarantine capability rather than treating newly learned material as automatically trusted. | [`6e6b927b`](https://github.com/Dadmin88/hermes-fleet/commit/6e6b927b2880aded5ba2be539fb5e73e7bfe7121), [`31bb7111`](https://github.com/Dadmin88/hermes-fleet/commit/31bb7111e6c4cfd555067224acc460e1a9d95336) |
| **2026-08-20** | The **Templar pre-execution gate** lands as a low-authority evaluator in the vNext execution path. | [`ad7a3ee9`](https://github.com/Dadmin88/hermes-fleet/commit/ad7a3ee9ba8838c50654b77586b7af8b63f977f3), [`1afb5eb4`](https://github.com/Dadmin88/hermes-fleet/commit/1afb5eb473c66a1176ffe4d6535066ab35e462c3) |

This timeline intentionally emphasizes durable architectural changes rather than every implementation commit. The repository’s complete Git history, issues, pull requests, phase ledger, and release history remain the authoritative detailed record.

## The vNext architecture

The current frozen direction is summarized by the project phrase:

> **durable brain, disposable body**

A persistent Hermes Agent Instance retains durable identity, its immutable Agency base, and only authorized promoted learning. A run receives a narrower temporary execution envelope:

```text
Principal
  → Persistent Hermes Agent Instance
  → Immutable RunAuthority
  → Temporary Run Capsule
  → Fleet-owned disposable execution body
  → Hermes native /v1/runs
  → finalization + quiescence + evidence
  → revoke temporary authority and destroy body
```

Two rules are especially important to the project’s identity:

1. **Authority may remain equal or narrow, never widen as execution crosses layers.**
2. **Cross-machine transport changes where work travels, not who is trusted or who owns the final truth.**

For the complete frozen ownership boundaries and invariants, read [`vnext-foundation.md`](vnext-foundation.md).

## How future provenance is recorded

For major architectural changes going forward, Fleet will prefer a simple public chain:

```text
proposal / ADR
    ↓
implementation issue or phase
    ↓
implementation PR + immutable merge commit
    ↓
verification / acceptance evidence
    ↓
release or canonical documentation
```

Architecture Decision Records live under [`docs/adr/`](adr/) and are timestamped by Git rather than backdated. Existing decisions made before the ADR convention are not rewritten as if an ADR existed at the time; this provenance timeline links directly to the original public evidence instead.

## Citing Hermes Fleet

When referring to the project, prefer an immutable commit, tagged release, or specific architecture document rather than a floating screenshot.

Suggested attribution:

> Hermes Fleet, by Kyle French / Dadmin88 — https://github.com/Dadmin88/hermes-fleet

For machine-readable citation metadata, see the repository’s [`CITATION.cff`](../CITATION.cff).
