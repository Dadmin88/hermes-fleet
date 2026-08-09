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

Inventory maps an exact friendly name to an immutable Keryx peer ID and operation policy. It should not contain transport URLs, passwords, node tokens, or TLS keys.

Validate that:

- names are unique;
- peer IDs are exact and current;
- allowed operations are intentional;
- deadlines and payload limits are bounded;
- local deny policy reflects the host's security expectations.

## Keryx requirements

Fleet depends on public Keryx behavior rather than Keryx database internals.

Before enabling Fleet traffic, verify:

- each local `keryxd` reports ready;
- edge nodes can authenticate to the intended relay;
- worker Fleet skills are visible through supported Keryx discovery;
- the controller can route to the exact worker peer;
- result delivery and durable task reattachment work on the deployed Keryx revision.

Do not use a service that reads or mutates Keryx SQLite directly as a Fleet fallback. Keryx's public daemon/SDK contracts are the supported boundary.

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

## Deployment sequence

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
12. Start `fleet-node` and verify advertised Fleet skills.
13. Start managed projection only if Nodescale integration is required.
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
- state and secret files have restrictive ownership and modes;
- logs do not contain tokens, prompts where prohibited, TLS private material, or API credentials.

## Updating

When updating Fleet or Keryx:

1. review changelogs and contract changes;
2. run the repository test gates on the candidate revisions;
3. update one boundary at a time where possible;
4. preserve durable state and identities;
5. rerun direct communication, deliberate execution, status reattachment, and deadline checks;
6. restart a running Hermes gateway after a Fleet plugin update so it loads the new plugin code.

Avoid treating a successful process restart as proof that cross-node request/result semantics still work.

## Rollback

Rollback should restore the previously known-good binaries and service configuration while preserving evidence and durable state for diagnosis.

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
