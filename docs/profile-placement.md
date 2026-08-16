# Profile identity, presence, and execution locality

Hermes Fleet treats professional profiles as **versioned capabilities whose current presence and exact identity must be proven rather than guessed**.

Hermes Agency defines and distributes professional profile packages. Hermes Agent installs and executes profiles in the current native runtime. Fleet observes what is actually installed on managed nodes, ties that evidence to current admission and readiness, and exposes deterministic read-only lookup facts.

The important split is:

> **Agency owns profile content. Fleet owns live distributed evidence and placement policy.**

This document distinguishes the current merged profile-awareness contracts from planned vNext execution. The frozen planned direction is [vNext foundation](vnext-foundation.md): Agency content becomes the immutable capability base of a persistent Hermes Agent Instance, while temporary execution authority lives in RunAuthority/Run Capsules rather than in profile installation. Persistent host profile installation is not a prerequisite for distributed work.

## Current product contract

Fleet currently provides:

- bounded discovery of installed Hermes profile distributions on a Fleet node;
- current profile presence carried in the node observation path;
- exact Agency V1 content identity when Fleet can safely prove it;
- general ready-node lookup by profile name and optional version;
- exact ready-node lookup by profile name, version, and content digest;
- pinned immutable Agency package/source validation at an exact git revision;
- deterministic read-only candidate discovery over currently admitted, scheduler-ready nodes.

These are observation and lookup capabilities. They do not install, update, remove, rank, reserve, schedule, or execute a profile by themselves.

## Responsibility split

| Concern | Owner |
| --- | --- |
| Professional role definition | Hermes Agency `SOUL.md` |
| Bundled role-specific procedures | Hermes Agency profile `skills/` |
| Distribution identity and package metadata | Hermes Agency |
| Installing a profile into a current native Hermes installation | Hermes Agent profile tooling |
| Detecting installed distributions on a Fleet node | Hermes Fleet node observation path |
| Current managed-node admission | Nodescale projected into Fleet |
| Current node readiness and Fleet-owned capacity | Hermes Fleet |
| Authenticated cross-node delivery | Hermes Keryx |
| Exact ready-node profile lookup | Hermes Fleet state |
| Future workload placement policy | Hermes Fleet |
| Future environment materialization | Fleet execution backend selected by a validated ExecutionPlan |

Agency is intentionally not a live node registry. It does not need to know which laptops, desktops, VPS hosts, phones, or remote workers currently carry one of its profiles.

## The three levels of profile identity

Fleet can encounter profile presence at different confidence levels.

### 1. Name

Example:

```text
agency-backend-engineer
```

A name is useful for general capability discovery, but it is not enough to prove exact package content.

### 2. Name + distribution version

Example:

```text
agency-backend-engineer
0.1.0
```

Version narrows identity further, but a version string by itself still does not prove that behavior-bearing package bytes are the approved bytes.

### 3. Name + version + content digest

For Agency V1 packages whose behavior-bearing content can be read safely, Fleet computes the `hermes-agency-profile-content.v1` digest.

The exact identity becomes:

```json
{
  "name": "agency-backend-engineer",
  "version": "0.1.0",
  "content_digest": "<64 lowercase SHA-256 hex characters>"
}
```

That is the identity used by Fleet's strongest exact-profile lookup and is suitable as an immutable profile input to future Recipe resolution.

## What the Agency V1 digest represents

Fleet's installed-profile scanner computes a deterministic SHA-256 digest over the profile's behavior-bearing package content. The digest schema is:

```text
hermes-agency-profile-content.v1
```

The material includes the profile identity plus supported behavior files such as:

- `SOUL.md`;
- files under `skills/`;
- `config.yaml` when present;
- `mcp.json` when present;
- cron content when present;
- `.no-bundled-skills` when present;
- executable-bit semantics for included files.

The scanner is bounded by profile count, manifest size, file count, per-file size, and total bytes. Unsafe, ambiguous, changing, over-bound, or non-Agency-shaped distributions are not assigned a fabricated exact digest.

A generic profile can therefore remain visible as `{name, version}` while omitting `content_digest`.

## Installed profile observation

Installed profile discovery runs as part of the existing Fleet node observation path. Fleet does not introduce a second profile-presence daemon or registry.

A current observation can express:

```text
no current observation
    -> profile presence unknown

current observation + profiles=[]
    -> node explicitly reports no discovered profile distributions

current observation + profiles=[...]
    -> node reports the listed installed distributions
```

Profile presence inherits the same admission and freshness boundaries as the observation that carries it. A stale or pre-readmission observation is not transformed into current placement authority.

## Readiness is not profile identity

Scheduler readiness and profile presence answer different questions.

A node may be:

```text
ready + exact package present
ready + same-name package but wrong digest
ready + requested package absent
not ready + exact package present
```

Fleet therefore never treats `scheduler_ready=true` as proof that the desired professional profile exists.

Likewise, an exact installed package does not make a node eligible when its observation is stale, Keryx is unavailable, Hermes is unavailable, the Fleet worker is unavailable, or Fleet-owned execution capacity is exhausted.

## Current lookup layers

### General profile lookup

A general profile query asks for scheduler-ready nodes that report a profile name, optionally at an exact version.

Use this when the caller cares about current native profile presence but does not require exact Agency content identity.

The query never substitutes a different professional profile when the requested profile is missing.

### Exact profile lookup

Exact lookup requires the requested profile content digest and can also require the exact distribution version.

It returns only currently admitted, fresh, scheduler-ready nodes whose current observation proves the requested identity.

Digestless or mismatching packages do not satisfy exact lookup.

### Candidate lookup

Fleet can also query currently scheduler-ready admitted nodes that may be considered by a later placement policy.

Each candidate carries facts such as:

- managed source/network/device identity;
- current admission generation;
- available Fleet worker slots;
- current scheduling resource observations;
- same-name installed profile presence when one exists;
- the current readiness explanation.

The query intentionally returns **all eligible candidates in deterministic order**. It does not rank or choose a winner.

That separation keeps persistence and inspection from quietly becoming an undocumented scheduler.

## Pinned Agency source snapshots

Exact package identity requires stronger source trust than a mutable repository branch.

Fleet has a pinned Agency snapshot boundary that binds an approved repository to an **exact full git object ID**. A resolved package is tied to that immutable checkout rather than a mutable branch or tag.

The current merged snapshot flow validates, among other things:

- the approved repository identity;
- an exact full git revision;
- the checked-out revision;
- bounded supported Agency catalog metadata;
- the supported `hermes-agency-profile-content.v1` digest schema;
- deterministic profile roster and identity;
- safe profile distribution paths;
- selected `distribution.yaml` name/version;
- an independently recomputed content digest for the selected profile package.

A snapshot resolves an `AgencyProfilePackage`. It does not install the package anywhere and does not authorize execution.

The snapshot implementation is suitable only for explicitly approved Agency source repositories and exact revisions under the current contract. Do not generalize it into an arbitrary repository/package executor.

## Persistent remote host installation is not the default future path

An earlier Fleet design proposed completing `locate-or-place` by adding a privileged operation that persistently installed a missing Agency profile into the destination host's Hermes profile directory, then waited for a fresh observation to prove installation.

That mutation path is **not a current Fleet contract and is no longer the default planned architecture**.

The planned Fleet Execution Fabric moves the abstraction above host installation:

```text
exact Agency package / agent requirement
        ↓
Fleet Recipe
        ↓
ResolvedRecipe
        ↓
backend-specific ExecutionPlan
        ↓
capability-aware node selection
        ↓
materialize a fresh worker environment
        ↓
Hermes performs one task
        ↓
result + artifacts through Keryx
        ↓
clean task-specific state
```

Under that model, a trusted compatible node does not need the professional profile persistently installed on its host beforehand. The Recipe carries the requirement, Fleet resolves the exact package/environment identity, and the selected execution backend materializes the environment the node can support.

Examples of possible backend classes include a Docker/OCI backend on normal Linux hosts and a weaker userspace/OCI backend on supported Android nodes. Those backends are planned architecture, not current shipped contracts.

## How current profile presence remains useful

The execution-fabric direction does not make current profile observation obsolete.

Existing native installations remain useful as:

- truthful operator inventory;
- exact current native-run capability evidence;
- compatibility information for existing `fleet.hermes.run` behavior;
- possible future cache/locality evidence;
- a way to distinguish exact approved packages from same-name or digestless installations.

A future scheduler may prefer already-local immutable ingredients when that improves time-to-useful-work, but locality is not authority and does not bypass Recipe requirements, node admission, execution guarantees, or local authorization.

## Admission generation still matters

Nodescale-managed admission can change across disable, removal, revocation, or re-admission.

Fleet observations are fenced by the current projection/admission generation. When managed state changes, old evidence cannot be reused as if it came from the new admission epoch.

Example:

```text
profile observed under admission generation N
    ↓
node is disabled or re-admitted as generation N+1
    ↓
old generation-N observation arrives late
```

The late observation must not become current profile or placement evidence for generation N+1.

Future Recipe scheduling and environment materialization should consume the same admission fence rather than inventing a separate node identity system.

## Guidance for operators

If you need a profile available for the **current native Fleet execution path** today:

1. install the profile with supported Hermes profile tooling on the intended machine;
2. allow the Fleet node observation loop to refresh;
3. inspect Fleet readiness/profile presence rather than assuming installation succeeded globally;
4. use exact content identity when the work requires an exact approved Agency package.

Do not treat a same-name profile or an old observation as an exact-placement guarantee.

The future Recipe execution path is separate and must not be represented as shipped until its contracts, implementation, tests, and operational proofs are merged.

## Guidance for coding agents

Preserve these current source-of-truth relationships:

```text
approved immutable Agency package
        ↓
current native Hermes installation, when used
        ↓
Fleet node observation
        ↓
Fleet durable current state
        ↓
readiness / exact-profile lookup
```

For planned vNext work, preserve this higher-level direction:

```text
approved immutable Agency capability base
        ↓
Fleet Recipe / ResolvedRecipe / ExecutionPlan
        ↓
capability-aware placement + local admission
        ↓
Persistent Hermes Agent Instance
        ↓
Immutable RunAuthority
        ↓
Temporary Run Capsule
        ↓
Fleet-owned disposable execution body
        ↓
Hermes native /v1/runs
        ↓
finalize + destroy body; preserve Agent Instance
```

If execution remains on one machine, this path stays local. Nodescale/Keryx are introduced only for a real inter-machine identity/trust/transport boundary.

Do not:

- make Agency maintain a live Fleet node registry;
- make Nodescale select professional profiles;
- make Keryx decide Fleet execution authorization;
- read Hermes, Keryx, or Nodescale private databases as a shortcut;
- rank placement candidates inside the persistence query;
- silently substitute a different profile when the requested identity is unavailable;
- add persistent remote host profile installation merely to complete the superseded locate-or-place plan;
- treat image/cache/profile locality as execution authority.

See [`../AGENTS.md`](../AGENTS.md) for the repository-wide agent contract.

## Related documentation

- [Ecosystem map](ecosystem.md)
- [Architecture](architecture.md)
- [Node observations and scheduler readiness](node-readiness.md)
- [Managed projection V1](managed-projection-v1.md)
- [Deployment](deployment.md)
