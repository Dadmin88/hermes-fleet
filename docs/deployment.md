# Fleet Deployment

This guide describes a generic private-network deployment of Hermes Fleet with a controller node, a worker node, and Hermes Keryx providing authenticated cross-node transport.

It intentionally avoids machine-specific hostnames, personal profile names, certificate dates, task identifiers, and rollback paths. Those belong in local operations records for the environment being deployed.

## Reference topology

```text
controller host
  -> Hermes Fleet CLI / model tools
  -> local keryxd on loopback
  -> local keryx-node
  -> authenticated Keryx relay over a private network
  -> worker keryx-node
  -> worker keryxd on loopback
  -> fleet-node dispatcher
       -> direct Fleet handlers
       -> local Hermes Runs API on loopback
```

Nodescale managed projection, when enabled, is a separate local-only path into the Fleet worker state and does not traverse Keryx.

## Service responsibilities

A typical controller host runs:

- `keryxd.service`
- `keryx-node.service`
- Hermes with the Fleet plugin enabled

A typical worker host runs:

- `keryxd.service`
- `keryx-node.service`
- `fleet-node.service`
- a local Hermes gateway / Runs API service when `fleet.hermes.run` is permitted
- `fleet-managed-projection.service` when Nodescale-managed state is enabled

A relay host runs:

- `keryx-relay.service`

The relay may share the worker host in small private deployments, but the security boundaries remain the same.

## Network boundaries

Recommended defaults:

- bind `keryxd` to loopback;
- bind the Hermes Runs API to loopback;
- expose relay listeners only on the intended private interface;
- use TLS for non-loopback Keryx control and registry endpoints;
- keep Fleet managed projection on a local Unix-domain socket only;
- do not expose Fleet execution handlers directly over the network.

Fleet does not require a public-internet listener.

## Secrets and identities

Never commit:

- Keryx node tokens;
- libp2p/node private keys;
- TLS private keys;
- Hermes API credentials;
- private environment files;
- operator inventory containing secret material.

Fleet inventory should identify nodes through stable Keryx peer IDs and operator-friendly names. Credentials belong in service-owned configuration outside the repository.

Persistent Keryx identities should normally survive reinstall or redeploy. Rotating an identity changes the peer identity Fleet targets and should be treated as an explicit lifecycle event.

## Suggested filesystem layout

A user-service deployment can use a layout such as:

```text
$HOME/.local/share/hermes-fleet/bin/       installed Keryx/Fleet executables
$HOME/.local/share/hermes-fleet/venv/      Python runtime when required
$HOME/.local/state/hermes-fleet/           Fleet and Keryx durable state
$HOME/.config/hermes-fleet/                non-Git service configuration
$HOME/.config/systemd/user/                 user service units
```

Hermes profile-specific Fleet inventory remains under the active Hermes profile state according to Hermes conventions.

The exact paths are deployment choices. Service units should reference configuration through environment files rather than embedding secrets in unit definitions.

## Fleet inventory

Initialize operator state with:

```bash
hermes fleet init
```

New schema-v2 configuration keys explicit operation policy by authoritative managed identity. The operator layer resolves the current authenticated Keryx peer from managed binding provenance, so operators can use an explicit alias, managed DeviceId, or Fleet stable ID instead of maintaining peer IDs as the primary human-facing target. Existing schema-v1 inventory remains supported by the static exact-peer controller surfaces. Neither form should contain transport URLs, passwords, node tokens, or TLS keys. See [Operator foundation](operator-foundation.md).

Validate that:

- names are unique;
- peer IDs are exact and current;
- allowed operations are intentional;
- deadlines and payload limits are bounded;
- local deny policy reflects the host's security expectations.

## Keryx requirements

Fleet depends on public Keryx behavior rather than Keryx database internals. The Python package and worker bundle pin the SDK and daemon/edge artifacts to immutable Keryx commit `1a569219517ea3f6ea216967f4dcc23dcaf5c822`. Worker convergence must verify artifact hashes from the bundle rather than infer provenance from package version or service health.

Before enabling Fleet traffic, verify:

- each local `keryxd` reports ready;
- edge nodes can authenticate to the intended relay;
- worker Fleet skills are visible through supported Keryx discovery;
- the controller can route to the exact worker peer;
- result delivery and durable task reattachment work on the deployed Keryx revision.

Do not use a service that reads or mutates Keryx SQLite directly as a Fleet fallback. Keryx's public daemon/SDK contracts are the supported boundary.

## Fleet node environment contract

The bundled worker reads its service environment from the site-owned `fleet-node.env`. A production environment must define:

- `FLEET_NODE_NAME`: one exact enabled name from the selected Fleet inventory;
- `FLEET_CONTROLLER_PEER_IDS`: a comma-separated, non-empty set of exact controller peer IDs used for sender authorization and controller-route observations;
- `KERYX_NODE_TOKEN`: the worker's secret Keryx node token;
- `API_SERVER_KEY`: the secret credential for the loopback Hermes Runs API.

Observation publishing is optional. Local publishing requires the atomic set `FLEET_OBSERVATION_SOCKET`, `NODESCALE_NETWORK_ID`, and `NODESCALE_DEVICE_ID`. Remote publishing instead requires `FLEET_REMOTE_OBSERVATION_ENDPOINT`, `FLEET_REMOTE_OBSERVATION_TARGET_PEER_ID`, `HERMES_KERYX_REGISTRY_CA_CERT`, `NODESCALE_NETWORK_ID`, and `NODESCALE_DEVICE_ID`. The remote endpoint must be `https://`; Fleet reuses Keryx's configured relay/registry CA trust material and never falls back to plaintext. The socket must match the Rust managed-control unit, and the two IDs must match an active managed projection. Keep the environment file outside Git with mode `0600`; do not place tokens in the unit or inventory.

The example unit's inventory path and profile are baseline deployment examples. Sites using another profile or layout must install a systemd drop-in that replaces `ExecStart` with exact absolute `--config` and `--binding-db` paths rather than copying machine-specific paths into the repository.

## Hermes Runs API

`fleet.hermes.run` requires a local Hermes Runs capability on the worker.

Recommended configuration:

- loopback bind only;
- authenticated requests;
- explicit health/capability probe support;
- bounded request/status/stop timeouts;
- no automatic approval of interactive or privileged requests.

Fleet should advertise executable capability only when the local integration can safely start, observe, and stop runs according to the configured policy.

## Managed projection

Nodescale integration is optional and local to the Fleet host.

The `fleet-managed-projection` service should use:

- a pre-provisioned private socket parent;
- a private Fleet-owned state directory;
- exact Linux peer-UID authentication;
- no TCP listener;
- no bearer token in the projection protocol.

See [Managed projection V1](managed-projection-v1.md) for the wire and authorization contract.

### Node observations

When managed projection and scheduler-readiness observation are enabled on a worker, the same Rust service also owns the bounded local observation interface and current observation state. Configure `fleet-node` with:

- `FLEET_OBSERVATION_SOCKET` matching the managed-control socket;
- `NODESCALE_NETWORK_ID` and `NODESCALE_DEVICE_ID` matching the active managed projection;
- an optional `--observation-interval` (30 seconds by default).

Configure `fleet-managed-control` with an optional `--freshness-seconds` (90 seconds by default). Keep the observation interval below the freshness window so a healthy publisher refreshes before expiry.

The Fleet node publishes after Keryx registration, on its periodic interval, and when Hermes execution capacity changes. Publication failure does not convert stale data into ready data: the last-known observation remains inspectable and becomes not-ready after the freshness window.

See [Node observations and scheduler readiness](node-readiness.md) for the field and reason model.

## Deployment sequence

Install the built Python wheel into the worker runtime. Install the Rust control binary at the path used by the example user unit with:

```bash
cargo install --locked --path crates/fleet-control --bin fleet-managed-control --root "$HOME/.local"
```

A conservative rollout sequence is:

1. Select exact Fleet and Keryx revisions for the deployment.
2. Run repository tests and build gates for those revisions.
3. Build/install Keryx binaries on participating hosts.
4. Install Fleet on controller and worker hosts.
5. Preserve existing valid node identities unless rotation is intentional.
6. Write relay security configuration and per-host environment files with restrictive permissions.
7. Initialize or update Fleet inventory with exact Keryx peer IDs.
8. Install service units and validate them with `systemd-analyze verify` where systemd is used.
9. Start the relay and verify health.
10. Start local Keryx daemons and edge nodes and verify authenticated connectivity.
11. Start the worker's local Hermes Runs API if executable Fleet operations are enabled.
12. Start managed projection first when Nodescale-managed state or node observations are enabled.
13. Start `fleet-node` and verify advertised Fleet skills and its first readiness observation.
14. Run the [integration verification](smoke-test.md).
15. Restart any already-running Hermes gateway that must load a newly installed Fleet plugin version.

Bring layers up one at a time so failures can be attributed to the correct boundary.

## Post-deployment checks

Verify:

- no service is restart-looping;
- only intended interfaces are listening;
- Keryx daemon and relay health checks pass;
- worker skills are registered by the intended owner;
- Fleet direct operations do not create Hermes runs;
- executable Fleet operations create at most one bound run per Keryx task;
- peer-originated output is presented as untrusted;
- managed projection cannot grant `fleet.hermes.run`;
- configured nodes report last observation time, freshness, worker capacity, and readiness reasons through health/inventory;
- stopping observation refresh makes the node stale and not-ready without deleting last-known state;
- state and secret files have restrictive ownership and modes;
- logs do not contain tokens, prompts where prohibited, TLS private material, or API credentials.

## Updating

When updating Fleet or Keryx:

1. review changelogs and contract changes;
2. run the repository test gates on the candidate revisions;
3. update one boundary at a time where possible;
4. before the new Rust managed-control service first opens any schema-v1 or schema-v2 Fleet database, stop that service and take a consistent SQLite backup that can be restored independently of the live WAL/SHM files;
5. preserve durable state and identities;
6. rerun direct communication, deliberate execution, status reattachment, and deadline checks;
7. restart a running Hermes gateway after a Fleet plugin update so it loads the new plugin code.

Avoid treating a successful process restart as proof that cross-node request/result semantics still work.

## Rollback

Rollback should restore the previously known-good binaries and service configuration while preserving evidence and durable state for diagnosis.

The readiness release upgrades the Fleet state database through schema 2 to schema 3. Schema 2 adds the current-observation table; schema 3 transactionally discards any pre-fence observation JSON so a fresh projection-generation-bound sample is required. A schema-1 or schema-2 binary must not be started against a migrated schema-3 database. Rolling the Rust service itself back therefore requires restoring the consistent pre-upgrade Fleet database backup together with the old binary. If the new Rust service remains installed, observation publishing can instead be disabled on `fleet-node` without removing the schema-3 table or last-known records.

Do not delete:

- Keryx databases;
- Fleet execution-binding state;
- managed projection state;
- node identities;
- logs needed to correlate a failed request;
- secret material that is still required for the restored deployment.

If an identity or credential must be rotated because compromise is suspected, treat that as incident response rather than an ordinary rollback.

## Operational hygiene

Environment-specific facts such as hostnames, exact IPs, current certificate expirations, service-account names, backup directories, rollout timestamps, and incident notes should live in private deployment records, not in this repository's durable documentation.
