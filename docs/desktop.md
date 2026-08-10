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

Provider observations are optional. To enable them for a profile-scoped Desktop or gateway, create `$HERMES_HOME/fleet/nodescale-observations.json` as a regular non-symlink file no larger than 4096 bytes:

```json
{
  "schema": "fleet.nodescale-observations.v1",
  "socket_path": "/absolute/private/path/observations.sock",
  "network_id": "00000000-0000-0000-0000-000000000000"
}
```

The closed schema rejects unknown or missing fields, relative socket paths, symlinks, oversized files, and malformed JSON. Process-level `NODESCALE_OBSERVATION_SOCKET` and `NODESCALE_OBSERVATION_NETWORK_ID` values remain supported and take precedence when both are present; an incomplete environment pair fails closed.

Use the real Nodescale network UUID. Nodescale and the Hermes backend must run as the same UID. The socket's canonical parent must be service-owned mode `0700`; the socket is mode `0600`. Do not loosen filesystem permissions or use a direct database path.

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


## Application structure

Fleet keeps exactly one global Hermes Desktop sidebar entry and owns its navigation inside the Fleet application shell. The internal routes are stable operator destinations rather than separate top-level Desktop plugins:

| Section | Route | Phase 0 behavior |
| --- | --- | --- |
| Overview | `/fleet` | Existing validated Fleet summary plus operator entry points. |
| Network | `/fleet/network` | Live managed/observed Canvas and node inspection. |
| Members | `/fleet/members` | Truthful shell; no membership mutation contract is inferred. |
| Invitations | `/fleet/invitations` | Truthful shell; no invitation secrets or mutations are exposed yet. |
| Profiles | `/fleet/profiles` | Truthful shell reserved for Fleet-owned profile presence/placement contracts. |
| Workflows | `/fleet/workflows` | Existing local editor, explicitly non-executing. |
| Activity | `/fleet/activity` | Truthful shell reserved for authoritative activity history. |
| Settings | `/fleet/settings` | Truthful shell; no backend policy is mutated yet. |

The responsive internal navigation collapses from a left rail to a horizontally scrollable section bar on narrow Desktop windows. Route changes do not create additional Hermes sidebar entries.

Workflow documents remain process-memory-only in this phase. Internal Fleet route changes preserve the current workflow editing session, including exact-machine targets created from Network selection, but a Desktop plugin reload still resets that session. This does not create a durable workflow backend or execution runtime.

## Runtime behavior

Open **Fleet** from the Hermes Desktop sidebar. The Fleet entry opens **Overview**; use the internal navigation for Network, Members, Invitations, Profiles, Workflows, Activity, and Settings.

The validated operational surfaces render:

- **Loading:** `Discovering nodes…` using the native Hermes loader;
- **Unavailable:** an explicit backend error with Retry while the Fleet navigation remains available;
- **Overview:** Managed, Observed, Alive, Ready, and Needs Attention counts;
- **Network:** real managed Fleet nodes with readiness/worker capacity plus distinct observed provider evidence;
- **Workflows:** the existing local non-executing workflow editor on its own route;
- **Reserved operator sections:** explicit explanatory shells that do not fabricate membership, invitation, profile, activity-history, or settings authority.

No fake nodes or scheduler metrics are seeded. Observed nodes have no rename, run, reservation, scheduler, readiness, or other authority-mutating controls. A selected observation may be copied into local Workflow Mode as an editor-only exact-machine target; that transition preserves `authority: observed`, remains runtime-unavailable, and grants no Fleet control or execution capability. Relationship edges remain absent because neither API supplies provenance-bearing relationship evidence.

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
