# Hermes Fleet Deployment Guide

This guide describes a generic private-network deployment with one controller node and one worker node. It intentionally uses placeholders and does not include real hostnames, peer IDs, credentials, addresses, task IDs, or operational evidence.

Hermes Fleet is experimental. Deploy it only on machines and networks you control.

## Reference topology

```text
controller-node
  -> Hermes Fleet CLI or model tools
  -> local keryxd on loopback
  -> Keryx edge node
  -> private network or authenticated Keryx relay
  -> worker Keryx edge node
  -> worker keryxd on loopback
  -> fleet-node dispatcher
  -> local Hermes Runs API on loopback
```

The controller may use Fleet without a long-running Fleet controller service. The worker requires a supervised `fleet-node` process. Remote Hermes execution additionally requires a local Hermes gateway exposing the authenticated Runs API.

## Prerequisites

On each participating machine:

- Linux with Python 3.11 or newer;
- a compatible Hermes Agent installation;
- a compatible Keryx daemon and SDK;
- Git access to the Fleet repository;
- systemd user services or another process supervisor;
- private network reachability between required Keryx endpoints;
- synchronized system clocks;
- owner-protected locations for keys, tokens, TLS material, and API credentials.

Freeze compatible Fleet, Keryx, and Hermes revisions before deploying. Do not assume arbitrary versions are interoperable.

## Security boundary

Recommended exposure:

| Component | Bind or exposure |
| --- | --- |
| Local Keryx daemon | Loopback only |
| Local Hermes Runs API | Loopback only |
| Fleet node dispatcher | Consumes through local Keryx; no public HTTP listener required |
| Keryx relay and registry | TLS over a private network |
| Keryx peer transport | Private-network address or explicitly secured relay path |

Never commit:

- Keryx node keys;
- node tokens;
- Headscale or Tailscale credentials;
- TLS private keys;
- Hermes API keys;
- model-provider credentials;
- real peer IDs or private topology evidence in reusable documentation.

## Runtime layout

The reference units assume a user-level installation using locations similar to:

```text
~/.local/share/hermes-fleet/bin/       Keryx release binaries
~/.local/share/hermes-fleet/venv/      Fleet runtime virtual environment
~/.local/state/hermes-fleet/           Keryx and Fleet durable state
~/.config/hermes-fleet/                configuration and secret environment files
~/.config/systemd/user/                installed user units
```

Fleet inventory belongs under the active Hermes home:

```text
$HERMES_HOME/fleet/nodes.yaml
```

When Hermes profiles use separate homes, each profile receives its own Fleet state and inventory.

## Install the Fleet plugin

On a controller or any Hermes environment that should expose Fleet CLI/model tools:

```bash
hermes plugins install Dadmin88/hermes-fleet --enable
hermes fleet init
```

Restart an existing gateway after installing or updating the plugin:

```bash
hermes gateway restart
```

## Configure Fleet inventory

Edit the active `nodes.yaml` with generic friendly names and immutable Keryx peer IDs:

```yaml
schema_version: 1

defaults:
  max_deadline_seconds: 300
  max_payload_bytes: 65536
  max_prompt_chars: 16000
  max_export_paths: 8

nodes:
  - name: worker-1
    peer_id: "<worker-keryx-peer-id>"
    tags: [worker]
    enabled: true
    priority: 0
    policy:
      allowed_operations:
        - fleet.health
        - fleet.inventory
        - fleet.message
      max_deadline_seconds: 120
      max_payload_bytes: 65536
      max_prompt_chars: 16000
      max_export_paths: 0
```

Enable `fleet.hermes.run` only after the worker's local Hermes Runs API, sender authorization, deadlines, and execution-binding path have been validated.

## Configure Keryx

Keryx configuration is outside Fleet's repository and must follow the Keryx version being deployed.

Typical local variables include:

```text
HERMES_KERYX_DAEMON_ADDR=127.0.0.1:50051
HERMES_KERYX_DATA_DIR=<owner-protected-state-directory>
HERMES_KERYX_DAEMON_PEER_ID=<local-peer-id>
```

The Rust edge service and Python SDK may require differently formatted daemon endpoint variables. Keep component-specific environment files separate and follow the exact documentation for the pinned Keryx release.

Configure relay, registry, allowlist, node-token, and TLS files with mode `0600` where applicable. Do not print their contents during installation or diagnostics.

## Configure the worker

The worker needs:

1. A local Keryx daemon.
2. A Keryx edge node connected to the selected relay or peer topology.
3. A local Hermes gateway when executable Fleet operations are enabled.
4. A `fleet-node` process using the worker's Fleet config and execution-binding database.

Reference units are provided in `ops/systemd/`. They are templates, not universal drop-in configuration. Review profile names, paths, environment files, service dependencies, and hardening directives before installing them.

Example installation:

```bash
mkdir -p ~/.config/systemd/user
cp ops/systemd/*.service ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/*.service
systemctl --user daemon-reload
```

Start one layer at a time and inspect each layer before continuing:

```text
relay or private peer path
→ local Keryx daemons
→ Keryx edge nodes
→ local Hermes Runs API
→ fleet-node
```

## Worker authorization

The current release uses configured Keryx controller membership plus local node operation policy. Keep the worker default-deny and admit only known controller peer IDs.

Granular destination-owned per-sender grants are under development. Until they are available, avoid sharing one worker with mutually untrusted controllers.

## Deployment verification

Before sending executable work, verify:

- exact Fleet, Keryx, and Hermes versions;
- private-network and TLS health;
- local daemon readiness;
- worker registration for the expected Fleet operations;
- authenticated sender identity at the worker;
- local Hermes health and capabilities;
- disabled legacy bridges or fallback services;
- writable owner-only execution-binding state;
- clean service logs without restart loops.

Then follow [Two-node smoke test](smoke-test.md).

## Upgrade procedure

1. Record deployed revisions and service states.
2. Back up configuration, state, units, and credentials without printing secrets.
3. Review release notes and compatibility requirements.
4. Upgrade one component and one machine at a time.
5. Restart only affected services.
6. Re-run direct-operation smoke tests before executable tests.
7. Confirm the running Hermes gateway loaded updated Fleet tools.
8. Preserve the previous binaries and units until acceptance completes.

Do not automatically retry ambiguous executable submissions during an upgrade test.

## Rollback

A safe rollback should:

1. Stop only services changed by the deployment.
2. Restore the previous reviewed binaries, environment files, and units.
3. Preserve Keryx databases, Fleet execution bindings, identities, logs, and evidence for investigation.
4. Re-run direct health and inventory checks.
5. Re-enable execution only after duplicate-prevention behavior is confirmed.

Never use a bridge that reads Keryx storage directly or fabricates terminal results as a rollback path.

## Operational notes

- CLI operations do not require a permanent Fleet controller daemon.
- A gateway already running before a plugin update must be restarted to load updated tools.
- Cross-node cancellation is not currently supported and fails closed.
- Peer-produced responses and model output remain untrusted even when the sender was authenticated.
- Store deployment-specific evidence outside the repository in an owner-protected location.
