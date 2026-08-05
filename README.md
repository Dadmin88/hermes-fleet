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

## Install as a Hermes plugin

Hermes Fleet is released as a standalone Git directory plugin. Install and
enable the repository through Hermes' public plugin manager, then initialize
the active profile's Fleet state:

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
hermes plugins list --plain --no-bundled
```

The repository is private, so Git must already be authorized for
`Dadmin88/hermes-fleet`. Restart a running gateway after installation or update:

```bash
hermes gateway restart
```

`init` creates missing state files without overwriting valid operator state.
The Git checkout installed by `hermes plugins install` is the supported Hermes
plugin artifact. The wheel built from `pyproject.toml` packages the
`hermes_fleet` Python library for development and integration use; it is not the
Hermes plugin installation path.

## Deliberately out of scope

Phase 1 has **no network traffic, Keryx SDK integration, discovery, health probing, dispatch, secrets, history/task database, daemon, or dashboard**. `fleet_list_nodes` is intentionally a non-networking placeholder; it does not expose live inventory or dispatch work.

Earlier direct Hermes A2A concepts are superseded design evidence only. Production Phase 1 makes no direct-A2A or transport assumptions. A later phase must explicitly integrate Keryx and define dispatch behavior before remote work can occur.
