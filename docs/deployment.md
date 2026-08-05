# Fleet Deployment

This is the first-release deployment for one Katana controller and one VPS worker.

## Topology

```text
Katana Hermes profile: katana
  -> Fleet controller/model tools
  -> local keryxd (loopback gRPC)
  -> Keryx relay on VPS over Tailscale
  -> VPS keryx-node
  -> VPS local keryxd (loopback gRPC)
  -> fleet-node dispatcher
  -> VPS Hermes admin profile Runs API (loopback HTTP)
```

Katana's local `vps` Hermes profile is a client-side route to the VPS gateway. The VPS server/default profile is `admin`; `hermes-fleet-api.service` therefore starts `hermes -p admin gateway run` and Fleet worker config lives under the VPS `admin` profile.

## Trust boundaries

- Keryx owns peer identity, relay transport, registry ownership, durable task/result state, route receipts, and mailbox behavior.
- Fleet owns friendly node names, bounded envelopes, exact-node selection, direct-vs-executable dispatch, policy, task-to-Hermes-run binding, and presentation.
- Hermes `admin` owns local model execution.
- Registry and relay-control gRPC use TLS when bound to the VPS Tailscale address.
- Node keys, node tokens, TLS private keys, and the Hermes API key never belong in Git.
- The Hermes Runs API binds only to `127.0.0.1:8642`.
- Keryx daemons bind only to loopback gRPC.
- The relay libp2p listener binds only to the VPS Tailscale address.

## Runtime layout

Both hosts:

```text
~/.local/share/hermes-fleet/bin/        Keryx release binaries
~/.local/share/hermes-fleet/venv/       Fleet worker/controller Python runtime
~/.local/state/hermes-fleet/             Keryx/Fleet durable state
~/.config/hermes-fleet/                  non-Git config and secret environment files
~/.config/systemd/user/                  installed user units
```

VPS only:

```text
~/.hermes/profiles/admin/fleet/nodes.yaml
~/.config/hermes-fleet/relay.toml
~/.config/hermes-fleet/allowlist.toml
~/.config/hermes-fleet/node-tokens.toml
~/.config/hermes-fleet/tls/
```

Katana only:

```text
~/.hermes/profiles/katana/fleet/nodes.yaml
```

## Services

| Host | Unit | Purpose |
|---|---|---|
| VPS | `keryx-relay.service` | TLS-authenticated registry/control plus tailnet libp2p relay |
| VPS | `keryxd.service` | local durable task/result daemon |
| VPS | `keryx-node.service` | relay edge delivery into the VPS daemon |
| VPS | `hermes-fleet-api.service` | loopback Runs API using the VPS `admin` profile |
| VPS | `fleet-node.service` | one explicit direct/executable dispatcher |
| Katana | `keryxd.service` | local controller durable task/result daemon |
| Katana | `keryx-node.service` | relay edge delivery/result route |

The controller itself is short-lived inside CLI/model tool calls; no separate Fleet controller daemon is required.

## Mandatory replacement gate

Katana's historical `keryx-task-bridge.service` directly reads Keryx SQLite and writes fabricated stub completions. It must be stopped and disabled before Fleet services are accepted. Do not preserve it as a fallback.

The historical refresh-loop units are also replaced by the supervised `keryx-node.service`; do not run two edge nodes with the same key and identity.

## Install sequence

1. Validate and freeze exact Fleet and Keryx source bytes.
2. Build Keryx release binaries from the accepted Keryx SHA.
3. Install the same binaries and pinned Python SDK revision on both hosts.
4. Install Fleet into an isolated runtime venv and install/enable the Fleet Hermes plugin on Katana.
5. Generate persistent node/relay identities and node tokens without printing secrets.
6. Create a private CA and VPS certificate for the Tailscale IP/DNS name; distribute only the CA certificate to Katana.
7. Write security-enabled relay TOML, allowlist, and external node-token file with mode `0600`.
8. Write per-host Keryx environment files with mode `0600`.
9. Write exact `nodes.yaml` files for Katana `katana` and VPS `admin` profile homes.
10. Install the units from `ops/systemd/`, run `systemd-analyze verify`, and reload the user manager.
11. Stop/disable historical bridge/refresh units on Katana.
12. Start in dependency order: relay, both daemons, both edge nodes, Hermes `admin` API, Fleet worker.
13. Verify health and registry ownership before any execution request.
14. Run both acceptance smokes in `docs/smoke-test.md`.

## Rollback

A rollback stops and disables only the new Fleet/Keryx units, restores prior unit files from timestamped backups, and leaves all new state/config in place for inspection. It must not re-enable `keryx-task-bridge.service`, because that service fabricates terminal results.

Do not delete Keryx databases, binding state, keys, TLS material, or logs during rollback. Preserve them until correlated acceptance evidence has been reviewed.
