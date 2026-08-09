# Node observations and scheduler readiness

Fleet does not treat managed membership as proof that a node can receive useful work. Managed membership, current operational evidence, and scheduler readiness are separate layers.

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

## Observation model

A node observation contains typed, bounded fields for:

- the observation timestamp;
- the active projection generation captured before sampling as an admission epoch;
- network reachability;
- Keryx, Hermes, and worker availability;
- active and maximum Fleet-owned execution-slot counts;
- CPU core count and optional normalized load;
- RAM, swap, and filesystem capacity;
- optional GPU and VRAM capacity.

GPU telemetry is optional. When `nvidia-smi` is available, the publisher runs one bounded two-second query and aggregates at most 32 devices into current total/available VRAM; failed, malformed, unsupported, or oversized output is omitted. Missing optional resource telemetry does not make a node unhealthy. Resource observations are retained for later placement policy, but this release does not apply implicit CPU, RAM, disk, GPU, or VRAM thresholds.

The existing Fleet node worker obtains Keryx signals through the public SDK. A successful, well-formed `list_peers()` call marks the local Keryx control path available. Network reachability requires a distinct non-local peer whose ID exactly matches one of the configured controller peer IDs and whose public SDK `connected` field is `true`. The always-present local self row and known-but-disconnected controller rows are not reachability evidence. The SDK does not expose a separate positive relay-routability fact through this call, so Fleet fails closed rather than inferring one. Failed or malformed peer inspection marks both facts unavailable instead of assuming health.

The observation payload cannot select or redefine node identity. Fleet binds it to an existing managed `source`/`network_id`/`device_id` selector at the local control boundary. Before collecting a sample, the publisher captures the active projection generation as a non-authoritative `admission_generation` token. Projection generation advances exactly once for every accepted managed transition, even when Nodescale legitimately reuses membership or binding metadata. Fleet invalidates current evidence on every applied projection and accepts a sample only while its admission generation remains current, so delayed pre-disable evidence cannot restore readiness after re-admission. Hostnames, addresses, tags, and telemetry content remain non-authoritative.

Fleet stores one current observation per managed node in the existing `fleet-state` SQLite database. A newer sample replaces the current sample. Exact replay is idempotent, conflicting equal timestamps are rejected, and out-of-order samples cannot replace newer state. The Python publisher increments only exact same-millisecond collisions; a lower sampled wall-clock value starts a new local clock epoch rather than being clamped to the prior future timestamp. If the service detects that its persisted timestamp is now beyond the bounded future-skew allowance after a wall-clock regression, the next otherwise-valid current sample rebases ordering instead of leaving the node permanently stale. Every applied managed projection deletes current evidence in the same transaction, and a sample whose admission generation differs from the current projection generation is rejected. Re-admission therefore requires evidence captured after the new admission epoch becomes active. The schema-2 to schema-3 migration transactionally discards pre-fence observation JSON rather than assigning it to the current epoch. The service does not build an unbounded telemetry history and does not encode heartbeat samples as Keryx tasks.

## Freshness

`fleet-managed-control` defaults to a 90-second freshness window. Operators can set `--freshness-seconds` to an explicit value between 1 second and 24 hours. The bundled Fleet node publisher defaults to a 30-second refresh interval and can set `--observation-interval` between 5 and 3,600 seconds.

Freshness is evaluated from Fleet's receipt time, not from an untrusted self-declared readiness flag. The exact boundary is inclusive: an observation whose age equals the freshness window is fresh; one millisecond beyond it is stale.

A stale observation remains inspectable as last-known state, including its timestamps, capacity, and resource facts. Staleness changes readiness; it does not delete evidence.

## Derived readiness

Fleet derives a structured result containing:

- `alive`;
- `fresh`;
- `scheduler_ready`;
- `observation_age_ms`;
- machine-readable `reasons`;
- the last observation;
- active, maximum, and available worker slots;
- current resource observations.

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

Multiple reasons can be returned when multiple observed layers are unavailable. A node that does not advertise `fleet.hermes.run` reports its Fleet worker unavailable and rejects that operation even when local policy would otherwise allow it. A node with zero available Fleet-owned execution slots is never scheduler-ready for another Fleet run. This is not a claim about global Hermes capacity or non-Fleet work. After process restart, durable `creating`, `running`, or `indeterminate` Hermes run bindings conservatively consume the Fleet-owned slot until their durable state becomes terminal; restart never resets uncertain capacity to free, and a distinct new execution is rejected while that slot remains consumed.

Readiness is recomputed from persisted facts and the current time. It is not stored as a second authoritative state value, and workers do not submit `ready: true`.

## Operator inspection

When node observation publishing is configured, existing `fleet.health` and `fleet.inventory` responses include an additive `readiness` object. Existing response fields remain unchanged. The worker and controller both normalize the exact nested readiness keys, enums, reasons, capacity arithmetic, and resource bounds; malformed or unknown nested data is omitted at the worker and rejected at the controller boundary. The controller continues to mark peer-originated response content as untrusted; controller-owned routing and managed identity are not replaced by the returned data.

The local Rust control interface also supports `fleet.node-observation.v1` requests for bounded observation publication and readiness inspection. It uses the same private Unix socket and Fleet state database as managed projection.

A Fleet node enables periodic publishing with all three identity settings:

- `--observation-socket` or `FLEET_OBSERVATION_SOCKET`;
- `--managed-network-id` or `NODESCALE_NETWORK_ID`;
- `--managed-device-id` or `NODESCALE_DEVICE_ID`.

The values must identify the active managed projection already stored by Fleet. Partial observation configuration is rejected.

## What readiness does not mean

Scheduler readiness means that the platform layers required for a future run are currently observed as available and that a worker slot remains. It does not:

- select a node;
- place or reserve a job;
- score resources;
- prove eligibility for a future execution profile;
- install or update profiles;
- authorize `fleet.hermes.run`;
- dispatch a remote Hermes Run.

Those concerns remain separate future layers. Managed projection still cannot grant `fleet.hermes.run`.
