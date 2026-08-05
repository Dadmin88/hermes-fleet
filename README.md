# Hermes Fleet

Hermes Fleet is a **Keryx-first Phase 1** standalone Hermes plugin. It provides a local, transport-independent domain boundary for selecting named Keryx peers and validating the work intended for them.

## What Phase 1 provides

- An operator-managed schema-v1 inventory at `HERMES_HOME/fleet/nodes.yaml`.
- A friendly, normalized node name mapped to an immutable opaque Keryx `peer_id`; no URL, credential, or direct transport field is part of the inventory.
- Per-peer default-deny operation policy and bounded deadline, payload, prompt, and export-path limits.
- Strict JSON envelopes for `fleet.health`, `fleet.inventory`, and `fleet.hermes.run`.
- Deterministic enabled-node selection by exact name or AND-matched tags, sorted by priority then name.
- Owner-safe local initialization of `nodes.yaml` and recoverable `cache.json` with private state permissions.
- A public `hermes fleet init` CLI command and `fleet_list_nodes` tool placeholder. Tool results retain the stable `{success,data,errors,warnings}` shape.

Enable this standalone plugin through Hermes' normal `plugins.enabled` configuration, then initialize the active profile's state:

```bash
hermes fleet init
```

`init` creates missing state files without overwriting valid operator state. The source repository root is the standalone Hermes plugin; the built wheel contains the `hermes_fleet` Python domain package, not a separately proven plugin-root installation format.

## Deliberately out of scope

Phase 1 has **no network traffic, Keryx SDK integration, discovery, health probing, dispatch, secrets, history/task database, daemon, or dashboard**. `fleet_list_nodes` is intentionally a non-networking placeholder; it does not expose live inventory or dispatch work.

Earlier direct Hermes A2A concepts are superseded design evidence only. Production Phase 1 makes no direct-A2A or transport assumptions. A later phase must explicitly integrate Keryx and define dispatch behavior before remote work can occur.
