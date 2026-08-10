# Fleet Desktop D1

Fleet Desktop D1 adds a native Hermes Desktop page backed by current managed-node state. It deliberately stops before the visual Canvas milestone: D1 proves plugin packaging, authoritative data projection, native loading/failure/empty states, overview counts, and a real node list.

## State boundary

The data path is:

```text
fleet-state → fleet-control local socket → hermes_fleet.desktop_api
           → Hermes dashboard plugin API → Hermes Desktop runtime plugin
```

The dashboard backend does not read Fleet SQLite directly. Readiness, freshness, managed state, and capacity remain Fleet domain decisions.

The Desktop API schema is `fleet.desktop.v1`. Its node identity is the stable Fleet identity tuple plus a deterministic opaque `stable_id`. The hash-derived `stable_id` is a durable correlation key, not anonymization or a secret; the endpoint therefore remains authenticated and local-only. Display names currently fall back to the managed `device_id` because managed projection V1 does not carry provider presentation metadata. `provider_name` and `alias` are explicit nullable fields so later provider naming and Fleet aliases do not change identity or break clients.

## Requirements

- current Hermes Agent/Desktop with runtime desktop-plugin support;
- Hermes Fleet installed as a Hermes plugin;
- `fleet-managed-control` running under the same local user as the Hermes gateway;
- its socket at `$HERMES_HOME/fleet/managed-projection.sock`, or `FLEET_MANAGED_PROJECTION_SOCKET` inherited by the gateway process;
- current managed projection and node observations.

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

D1 renders:

- **Loading:** `Discovering nodes…` using the native Hermes loader;
- **Unavailable:** an explicit backend error with Retry;
- **Empty:** an explicit empty-Fleet explanation;
- **Overview:** Managed, Alive, Ready, and Needs Attention counts;
- **Nodes:** real managed nodes with current readiness and worker capacity.

No fake nodes or scheduler metrics are seeded. D1 does not expose rename, Canvas, workflow, run, reservation, scheduler, or Sentinel controls.

## Troubleshooting

### Fleet is unavailable

1. Verify `fleet-managed-control` is running.
2. Verify the configured socket exists and is owned by the expected local service user.
3. Verify the gateway process resolves the same `HERMES_HOME`.
4. If using a nonstandard socket, ensure the gateway process inherits `FLEET_MANAGED_PROJECTION_SOCKET`.
5. Restart the gateway after backend plugin installation or changes.

The HTTP route intentionally returns a bounded `503` message rather than leaking local paths or backend implementation details.

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
python -m pytest -q tests/unit/test_desktop_api.py \
  tests/unit/test_desktop_plugin_api.py \
  tests/unit/test_desktop_plugin_assets.py
cargo test --locked -p fleet-state --test desktop
cargo test --locked -p fleet-control --test observations
node --check desktop/plugin.js
```

Run the repository's complete CI bundle before opening a pull request.
