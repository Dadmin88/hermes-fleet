# Fleet Desktop

Fleet Desktop is a native Hermes Desktop page backed by current managed Fleet authority and a strictly separate read-only provider-observation layer.

## State boundary

The data path is:

```text
fleet-state → fleet-control local socket → hermes_fleet.desktop_api
Nodescale state → Nodescale observation UDS → hermes_fleet.nodescale_observations
            both → Hermes dashboard plugin API → Hermes Desktop runtime plugin
```

The dashboard backend reads neither Fleet nor Nodescale SQLite directly. Readiness, freshness, managed state, and capacity remain Fleet domain decisions. Provider observations remain Nodescale evidence and never become Fleet admission, readiness, aliases, reservations, scheduling, execution bindings, operations, or `fleet.hermes.run` authority.

The managed control API remains `fleet.desktop.v1`. The authenticated dashboard composes it with validated `nodescale.observations.v1` evidence as `fleet.desktop.v2`. Managed nodes stay in `nodes`; observed/unmanaged nodes are in the distinct `observed_nodes` collection with separate observation status and reconciliation evidence.

Managed identity is the stable Fleet identity tuple plus its deterministic opaque `stable_id`. Observed identity is a provider-owned opaque digest derived by Nodescale from provider kind, provider instance, provider node ID, and stable provider fingerprint. Fleet does not deduplicate observed and managed rows by name, IP address, tag, or other heuristic. The two rows remain distinct until provenance-bearing exact identity evidence exists.

## Requirements

- current Hermes Agent/Desktop with runtime desktop-plugin support;
- Hermes Fleet installed as a Hermes plugin;
- `fleet-managed-control` running under the same local user as the Hermes gateway;
- its socket at `$HERMES_HOME/fleet/managed-projection.sock`, or `FLEET_MANAGED_PROJECTION_SOCKET` inherited by the gateway process;
- current managed projection and node observations.

Provider observations are optional. To enable them, the Nodescale observation service and Hermes gateway must run as the same UID and the gateway must inherit both:

```bash
export NODESCALE_OBSERVATION_SOCKET="/absolute/private/path/observations.sock"
export NODESCALE_OBSERVATION_NETWORK_ID="00000000-0000-0000-0000-000000000000"
```

Use the real Nodescale network UUID. The socket's canonical parent must be service-owned mode `0700`; the socket is mode `0600`. Do not loosen filesystem permissions or use a direct database path.

The control socket remains peer-credential protected. Do not make it world-readable to enable Desktop access.

## Development install

Use a generic source checkout path; do not commit local paths into the repository.

```bash
export FLEET_REPO=/path/to/hermes-fleet
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
hermes plugins install "$FLEET_REPO"
mkdir -p "$HERMES_HOME/desktop-plugins/hermes-fleet"
ln -sfn "$FLEET_REPO/desktop/plugin.js" \
  "$HERMES_HOME/desktop-plugins/hermes-fleet/plugin.js"
hermes gateway restart
```

Hermes Desktop watches disk-plugin files. Saving `desktop/plugin.js` reloads the frontend plugin without rebuilding Desktop. Python backend or dashboard-manifest changes require reinstalling/updating the Hermes plugin and restarting the gateway so backend discovery runs again.

For a conventional socket location, configure `fleet-managed-control` with:

```bash
export FLEET_MANAGED_PROJECTION_SOCKET="$HERMES_HOME/fleet/managed-projection.sock"
```

The gateway only needs that variable when the socket is not at the conventional path.

## Runtime behavior

Open **Fleet** from the Hermes Desktop sidebar.

Fleet Desktop renders:

- **Loading:** `Discovering nodes…` using the native Hermes loader;
- **Unavailable:** an explicit backend error with Retry;
- **Empty:** an explicit empty-Fleet explanation;
- **Overview:** Managed, Observed, Alive, Ready, and Needs Attention counts;
- **Managed nodes:** real Fleet nodes with current readiness and worker capacity;
- **Observed nodes:** distinct dashed cards marked **Observed · unmanaged**, with provider identity and observation evidence only.

No fake nodes or scheduler metrics are seeded. Observed nodes have no rename, workflow, run, reservation, scheduler, or readiness controls. Relationship edges remain absent because neither API supplies provenance-bearing relationship evidence.

## Troubleshooting

### Fleet is unavailable

1. Verify `fleet-managed-control` is running.
2. Verify the configured socket exists and is owned by the expected local service user.
3. Verify the gateway process resolves the same `HERMES_HOME`.
4. If using a nonstandard socket, ensure the gateway process inherits `FLEET_MANAGED_PROJECTION_SOCKET`.
5. Restart the gateway after backend plugin installation or changes.

The HTTP route intentionally returns a bounded `503` message rather than leaking local paths or backend implementation details.

### Observations are unavailable

Managed Fleet state remains visible when the optional Nodescale socket is disabled, misconfigured, or unavailable. Check that both observation variables are present, the network ID is the intended UUID, Nodescale and the gateway share the same UID, and the socket path/parent permissions match the strict Nodescale contract. Errors are intentionally sanitized.

### Fleet page is missing

Verify the local file exists at:

```text
$HERMES_HOME/desktop-plugins/hermes-fleet/plugin.js
```

Then open Hermes Desktop plugin inventory and confirm `hermes-fleet` loaded without an unsupported-import or syntax error.

### No nodes appear

An empty page means the authoritative managed projection currently has zero managed rows. It is different from a missing observation: a managed machine without current evidence remains visible and reports an awaiting-evidence/not-ready state.

## Focused validation

```bash
python -m pytest -q tests/unit/test_nodescale_observations.py \
  tests/unit/test_desktop_api.py \
  tests/unit/test_desktop_plugin_api.py \
  tests/unit/test_desktop_plugin_assets.py
cargo test --locked -p fleet-state --test desktop
cargo test --locked -p fleet-control --test observations
node --check desktop/plugin.js
```

Run the repository's complete CI bundle before opening a pull request.
