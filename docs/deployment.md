# Fleet Deployment

This document describes the accepted v0.1 deployment with Katana as controller and the VPS as worker and relay host.

## Accepted revisions

- Fleet runtime acceptance SHA: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`
- Fleet CI: run `31062104463`, passed
- Keryx deployed SHA: `f4ee645e415600a959ea8062d1143140bd6c2616`
- Keryx integration PR: [Dadmin88/hermes-keryx#36](https://github.com/Dadmin88/hermes-keryx/pull/36)
- Hermes compatibility SHA used by Fleet CI: `a991dfc25daf68994c21d6adcdfbafb1b3dc23cf`

Documentation-only commits may advance Fleet `main` beyond the runtime acceptance SHA without changing deployed Python behavior.

## Topology

```text
Katana Hermes profile: katana
  -> Fleet CLI/model tools
  -> local keryxd on loopback
  -> Katana keryx-node
  -> Keryx relay on the VPS over Tailscale
  -> VPS keryx-node
  -> VPS local keryxd on loopback
  -> fleet-node dispatcher
  -> VPS Hermes admin profile Runs API on loopback HTTP
```

Katana's local `vps` Hermes profile is a client-side route to the VPS gateway. The VPS worker uses the `admin` profile. `hermes-fleet-api.service` therefore starts the Hermes gateway with `-p admin`, and Fleet worker state belongs to the VPS `admin` profile.

## Current service state

### Katana - active and enabled

- `keryxd.service`
- `keryx-node.service`

### Katana - disabled and inactive

- `keryx-task-bridge.service`
- `keryx-node-refresh.service`

The historical task bridge read Keryx SQLite directly and fabricated terminal completions. It must remain disabled and must never be used as a fallback.

### VPS - active and enabled

- `keryx-relay.service`
- `keryxd.service`
- `keryx-node.service`
- `hermes-fleet-api.service`
- `fleet-node.service`

The accepted rollout reported zero service restart loops after activation.

## Trust and network boundaries

- Keryx owns peer identity, relay transport, registry ownership, durable task/result state, route receipts, and mailbox behavior.
- Fleet owns friendly node names, bounded envelopes, exact-node selection, direct-vs-executable dispatch, policy, task-to-Hermes-run binding, and presentation.
- Hermes `admin` owns local model execution.
- Registry and relay-control gRPC use TLS when bound to the VPS Tailscale address.
- Node keys, node tokens, TLS private keys, and the Hermes API key never belong in Git.
- The Hermes Runs API binds only to `127.0.0.1:8642`.
- Keryx daemons bind only to loopback gRPC.
- The relay libp2p listener binds only to the VPS Tailscale address.
- `fleet-node` is the sole Fleet skill-registration owner. The Rust edge service remains delivery-only.
- `fleet.hermes.run` is advertised only when the local Runs capability probe confirms authenticated submission, status, and stop support.
- Peer-originated direct responses and model text remain untrusted even when Keryx authenticated the sender.

## Runtime layout

Both hosts use:

```text
~/.local/share/hermes-fleet/bin/        Keryx release binaries
~/.local/share/hermes-fleet/venv/       Fleet worker/runtime Python environment where required
~/.local/state/hermes-fleet/            Keryx and Fleet durable state
~/.config/hermes-fleet/                 non-Git configuration and secret environment files
~/.config/systemd/user/                  installed user units
```

VPS-specific paths:

```text
~/.hermes/profiles/admin/fleet/nodes.yaml
~/.config/hermes-fleet/relay.toml
~/.config/hermes-fleet/allowlist.toml
~/.config/hermes-fleet/node-tokens.toml
~/.config/hermes-fleet/tls/
```

Katana-specific paths:

```text
~/.hermes/profiles/katana/fleet/nodes.yaml
~/.hermes/profiles/katana/plugins/hermes-fleet/
```

## Runtime compatibility contracts

- `keryxd` uses `HERMES_KERYX_DAEMON_ADDR=127.0.0.1:50051`, `HERMES_KERYX_DATA_DIR=<directory>`, and `HERMES_KERYX_DAEMON_PEER_ID=<peer-id>`.
- Rust `keryx-node` uses `HERMES_KERYX_DAEMON_ENDPOINT=http://127.0.0.1:50051`; Python Keryx uses `HERMES_KERYX_DAEMON_ENDPOINT=127.0.0.1:50051` without a URI scheme. Keep them in separate environment files.
- Rust edge identity/bootstrap variables include `HERMES_KERYX_NODE_KEYPAIR_PATH`, `HERMES_KERYX_NODE_BOOTSTRAP_PEERS`, and `HERMES_KERYX_REGISTRY_CA_CERT`.
- Exact-peer Fleet submissions include canonical Keryx metadata `skill=<Fleet operation>` so the destination worker's accepted-skill filter can claim them.
- Relay-control and registry gRPC use the Tailscale-managed certificate for `hermes.tail6c8d50.ts.net`. Python gRPC receives the public full chain through `HERMES_KERYX_REGISTRY_CA_CERT`; the relay private key remains on the VPS.
- Fleet inventory files use `schema_version: 1`, `max_deadline_seconds`, `max_payload_bytes`, `max_prompt_chars`, and `max_export_paths`.

## Reinstall or redeploy sequence

1. Freeze exact Fleet and Keryx source SHAs.
2. Confirm the Keryx revision contains the public SDK surfaces required by Fleet.
3. Build Keryx release binaries and install the same accepted revision on both hosts.
4. Install Fleet into the worker runtime and install/enable the Fleet Hermes plugin on Katana.
5. Preserve valid persistent node and relay identities. Generate replacement credentials only when necessary and never print them.
6. Write security-enabled relay configuration, allowlist, node-token file, and per-host environment files with mode `0600`.
7. Write exact `nodes.yaml` files for the Katana `katana` and VPS `admin` profile homes.
8. Install units from `ops/systemd/`, run `systemd-analyze verify`, and reload the user manager.
9. Confirm the historical bridge and refresh-loop units are disabled before Fleet traffic.
10. Start and verify one layer at a time: VPS relay, both daemons, both edge nodes, VPS Hermes `admin` API, then VPS `fleet-node`.
11. Verify peer identity, registry ownership, service health, and log cleanliness.
12. Run the procedures in `docs/smoke-test.md`.
13. Restart the Katana Hermes gateway at a safe time after a plugin update so the running gateway loads the seven Fleet model tools.

## Operational notes

- CLI operations are available without a dedicated long-running Fleet controller daemon.
- A Hermes gateway process that was already running before Fleet installation or update must be restarted before model tools appear in that process.
- The deployed Tailscale TLS certificate expires on `2026-09-17`. Renew it before that date and restart `keryx-relay.service` after installing the renewed certificate.
- Periodic relay AutoNAT `NoServer` probe notices are nonblocking in the accepted private relay topology. Authenticated relay routing was proven live.
- Cross-node cancellation remains unavailable. Fleet returns an explicit error rather than claiming the remote Hermes run stopped.
- `node_service.py` may call `node.stop()` twice during normal shutdown. This is nonblocking cleanup outside the accepted request paths.

## Rollback snapshots

- Katana: `/home/kyle/.local/state/hermes-fleet/backups/20260806T000339Z-pre-deploy`
- VPS: `/home/dadmin/.local/state/hermes-fleet/backups/20260806T000341Z-pre-deploy`

## Rollback

A rollback stops and disables only the Fleet/Keryx units introduced or replaced by this deployment, restores prior unit files from the timestamped backups, and leaves new state/config in place for inspection.

Do not re-enable `keryx-task-bridge.service`. It fabricates terminal results and is not a safe fallback.

Do not delete Keryx databases, Fleet binding state, identities, keys, TLS material, or logs while investigating a rollback. Preserve them until correlated task, route, run, and result evidence has been reviewed.
