# Node observations and scheduler readiness

Fleet does not treat managed membership as proof that a node can receive useful work. Managed membership, current operational evidence, scheduler readiness, and installed profile presence are separate layers.

## Readiness layers

Fleet evaluates these facts in order:

1. **Managed state**: Nodescale has admitted the stable network/device identity and the projection is active.
2. **Observation freshness**: the Fleet worker has refreshed its operational sample within the configured freshness window.
3. **Reachability**: the worker reports that its required local network/control path is reachable.
4. **Keryx transport**: Keryx is available for Fleet work delivery.
5. **Hermes runtime**: Hermes can start, inspect, and stop Runs work.
6. **Worker state**: the Fleet worker is available.
7. **Fleet execution capacity**: at least one configured Fleet-owned execution slot is free.

A node can therefore be known but offline, alive without Hermes, healthy but saturated, or scheduler-ready. Liveness means that the latest observation is fresh. It does not mean that every required execution layer is available.

A scheduler-ready node also does not necessarily carry the professional Hermes profile required by a particular task. Profile presence is observed separately and can be combined with readiness through Fleet's profile lookup queries.

## Observation model

A node observation contains typed, bounded fields for:

- the observation timestamp;
- the active projection generation captured before sampling as an admission epoch;
- network reachability;
- Keryx, Hermes, and worker availability;
- active and maximum Fleet-owned execution-slot counts;
- installed Hermes profile distribution identities;
- CPU core count and optional normalized load;
- RAM, swap, and filesystem capacity;
- optional GPU and VRAM capacity.

### Installed profile presence

The existing Fleet worker scans installed Hermes profile distributions as part of the observation cycle. It does not create a separate live profile registry or heartbeat process.

Each safely discovered distribution has a canonical name and version. When the installed profile has the supported Hermes Agency V1 behavior shape, Fleet also computes an exact `hermes-agency-profile-content.v1` SHA-256 content digest from its behavior-bearing package files.

The observation therefore distinguishes:

```text
profiles unavailable because there is no current observation
```

from:

```text
profiles=[]
```

which means a current observation explicitly reports no discovered distributions, and from:

```text
profiles=[{name, version, optional content_digest}, ...]
```

which reports current installed presence.

A missing digest does not mean the profile is absent. It means Fleet cannot claim exact Agency V1 package identity for that installed distribution under the bounded scanner contract. Digestless presence can satisfy general name/version inspection but cannot satisfy an exact content-digest lookup.

Profile presence inherits the same admission and observation lifetime as the rest of the sample. Fleet does not trust a profile list from an arbitrary Keryx task response as current placement truth.

### Resource telemetry

GPU telemetry is optional. When `nvidia-smi` is available, the publisher runs one bounded two-second query and aggregates at most 32 devices into current total/available VRAM; failed, malformed, unsupported, or oversized output is omitted. Missing optional resource telemetry does not make a node unhealthy.

Resource observations are retained for placement policy, but the readiness derivation itself does not apply implicit CPU, RAM, disk, GPU, or VRAM thresholds. Placement-candidate inspection can expose these resource facts to a separately defined future policy without hiding ranking behavior inside readiness.

### Keryx and network evidence

In local observation mode, the Fleet node worker obtains Keryx signals through the public SDK. A successful, well-formed `list_peers()` call marks the local Keryx control path available. Network reachability requires a distinct non-local peer whose ID exactly matches one of the configured controller peer IDs and whose public SDK `connected` field is `true`.

In authenticated remote observation mode, successful authority acquisition from the exact configured controller is the network and Keryx evidence for that publication sample. Failed acquisition publishes no fresh sample. Fleet does not use the daemon-local peer directory to describe this separate direct-control route.

The always-present local self row and known-but-disconnected controller rows are not reachability evidence. The SDK does not expose a separate positive relay-routability fact through this call, so Fleet fails closed rather than inferring one. Failed or malformed peer inspection marks both facts unavailable instead of assuming health.

### Admission fencing

The observation payload cannot select or redefine node identity. Fleet binds it to an existing managed `source`/`network_id`/`device_id` selector at the local control boundary.

Before collecting a sample, the publisher captures the active projection generation as a non-authoritative `admission_generation` token. Projection generation advances exactly once for every accepted managed transition, even when Nodescale legitimately reuses membership or binding metadata.

Fleet invalidates current evidence on every applied projection and accepts a sample only while its admission generation remains current, so delayed pre-disable evidence cannot restore readiness or profile presence after re-admission. Hostnames, addresses, tags, telemetry content, and profile names remain non-authoritative for node identity.

Fleet stores one current observation per managed node in the existing `fleet-state` SQLite database. A newer sample replaces the current sample. Exact replay is idempotent, conflicting equal timestamps are rejected, and out-of-order samples cannot replace newer state.

The Python publisher increments only exact same-millisecond collisions; a lower sampled wall-clock value starts a new local clock epoch rather than being clamped to the prior future timestamp. If the service detects that its persisted timestamp is now beyond the bounded future-skew allowance after a wall-clock regression, the next otherwise-valid current sample rebases ordering instead of leaving the node permanently stale.

Every applied managed projection deletes current evidence in the same transaction, and a sample whose admission generation differs from the current projection generation is rejected. Re-admission therefore requires evidence captured after the new admission epoch becomes active. The schema-2 to schema-3 migration transactionally discards pre-fence observation JSON rather than assigning it to the current epoch.

The service does not build an unbounded telemetry or profile-presence history and does not encode heartbeat samples as Keryx tasks.

## Freshness

`fleet-managed-control` defaults to a 90-second freshness window. Operators can set `--freshness-seconds` to an explicit value between 1 second and 24 hours. The bundled Fleet node publisher defaults to a 30-second refresh interval and can set `--observation-interval` between 5 and 3,600 seconds.

Freshness is evaluated from Fleet's receipt time, not from an untrusted self-declared readiness flag. The exact boundary is inclusive: an observation whose age equals the freshness window is fresh; one millisecond beyond it is stale.

A stale observation remains inspectable as last-known state, including its timestamps, capacity, resources, and profile inventory. Staleness changes readiness and placement eligibility; it does not delete last-known evidence.

## Derived readiness

Fleet derives a structured result containing:

- `alive`;
- `fresh`;
- `scheduler_ready`;
- `observation_age_ms`;
- machine-readable `reasons`;
- the last observation;
- active, maximum, and available worker slots;
- current resource observations;
- current observed profile presence when an observation exists.

Possible not-ready reasons include:

- `node_unknown`;
- `node_not_active`;
- `observation_missing`;
- `observation_stale`;
- `observation_time_invalid`;
- `network_unreachable`;
- `keryx_unavailable`;
- `hermes_unavailable`;
- `worker_unavailable`;
- `no_worker_capacity`.

Multiple reasons can be returned when multiple observed layers are unavailable.

A node that does not advertise `fleet.hermes.run` reports its Fleet worker unavailable and rejects that operation even when local policy would otherwise allow it. A node with zero available Fleet-owned execution slots is never scheduler-ready for another Fleet run. This is not a claim about global Hermes capacity or non-Fleet work.

After process restart, durable `creating`, `running`, or `indeterminate` Hermes run bindings conservatively consume the Fleet-owned slot. Bounded reconciliation retains current work fail-closed, but marks indeterminate history resolved when the Keryx task is terminal and the exact known Hermes run is terminal or absent. A runless indeterminate creation additionally requires a five-minute uncertainty grace period. Resolved rows remain as audit history without consuming capacity.

Readiness is recomputed from persisted facts and the current time. It is not stored as a second authoritative state value, and workers do not submit `ready: true`.

## Readiness and profile placement

Fleet now has read-only state queries that combine current profile presence with the existing readiness truth.

### General ready-profile lookup

Fleet can find active, fresh, scheduler-ready nodes that advertise a requested Hermes profile name, optionally at an exact distribution version.

### Exact ready-profile lookup

Fleet can require the exact `content_digest` for an Agency V1 package. Digestless or mismatching installed profiles are excluded rather than being treated as equivalent.

### Placement candidates

When no exact carrier exists, Fleet can return all currently scheduler-ready admitted nodes that could be considered as destinations for the requested profile package. Candidate facts include current admission generation, Fleet worker capacity, resource observations, same-name installed presence when any, and readiness explanation.

This query does **not** rank, reserve, choose, install, update, or remove anything. It is a read-only view over the same durable managed/observation state.

Automatic remote profile installation and complete locate-or-place coordination remain outside the current operation surface. See [Profile identity and placement](profile-placement.md).

## Operator inspection

When node observation publishing is configured, existing `fleet.health` and `fleet.inventory` responses include an additive `readiness` object and current profile-presence data through the validated readiness/observation contract. Existing response fields remain compatible.

The worker and controller normalize the supported nested readiness keys, enums, reasons, capacity arithmetic, resources, and profile inventory bounds. Malformed or unknown nested data is omitted or rejected according to the boundary contract rather than promoted into authority.

The controller continues to mark peer-originated response content as untrusted; controller-owned routing and managed identity are not replaced by returned data.

The local Rust control interface also supports `fleet.node-observation.v1` requests for bounded observation publication and readiness inspection. It uses the same private Unix socket and Fleet state database as managed projection.

A Fleet node enables periodic publishing with all three identity settings:

- `--observation-socket` or `FLEET_OBSERVATION_SOCKET`;
- `--managed-network-id` or `NODESCALE_NETWORK_ID`;
- `--managed-device-id` or `NODESCALE_DEVICE_ID`.

The values must identify the active managed projection already stored by Fleet. Partial observation configuration is rejected.

For authenticated remote publication, replace the local observation socket with an
`https://` `FLEET_REMOTE_OBSERVATION_ENDPOINT`, an exact
`FLEET_REMOTE_OBSERVATION_TARGET_PEER_ID`, and Keryx's existing
`HERMES_KERYX_REGISTRY_CA_CERT`. Missing trust material is a configuration error;
invalid trust material fails the TLS connection, with no plaintext fallback.

## What readiness does not mean

Scheduler readiness means that the platform layers required for a future Fleet run are currently observed as available and that a worker slot remains. It does not:

- select a winning node among several eligible candidates;
- reserve a job or capacity slot for a future scheduler decision;
- apply an implicit resource score or threshold;
- prove that a requested Hermes profile is installed;
- prove that a same-name profile has the exact desired Agency content digest;
- install or update profiles;
- authorize `fleet.hermes.run` or any future privileged profile mutation;
- dispatch a remote Hermes Run.

Profile-aware lookup is now a separate current layer built on top of readiness and observed presence. Profile mutation and complete locate-or-place orchestration remain separate future layers.
