# Fleet Operator CLI V1

The top-level `fleet` command is a presentation adapter over the shared `OperatorService`. It does not read SQLite, recompute readiness, resolve hostnames, or create authority.

## Commands

```text
fleet nodes [--json]
fleet node show TARGET [--json]
fleet readiness TARGET [--json]
fleet run TARGET PROMPT [--wait|--detach] [--deadline SECONDS] [--json]
fleet task show TASK_ID [--json]
fleet doctor [--json]
```

`TARGET` is resolved by the operator foundation from an explicit alias, authoritative managed DeviceId, or Fleet stable ID. The current authenticated Keryx peer remains internal routing and diagnostic data.

`fleet run` waits by default. `--detach` returns the durable Keryx task identity after submission without claiming completion. Operators can later use `fleet task show TASK_ID`.

All commands consume the canonical active configuration at `$HERMES_HOME/fleet/nodes.yaml`. The managed-control socket defaults to `$HERMES_HOME/fleet/managed-projection.sock` and may use the existing `FLEET_MANAGED_PROJECTION_SOCKET` deployment override. Authenticated Keryx control uses the existing `KERYX_NODE_TOKEN` secret configuration.

## JSON

`--json` serializes the structured operator models directly. Human text is never parsed to produce JSON.

Errors contain a stable category and sanitized message. Debug details remain internal and are not printed by normal CLI output.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Success |
| 2 | Command-line usage error |
| 3 | Target not found or ambiguous |
| 4 | Policy or authentication denied |
| 5 | Target state is stale, not ready, or has no capacity |
| 6 | Required Fleet/Keryx/Hermes service or operation is unavailable |
| 7 | Remote task failed, was rejected, or exceeded its deadline |
| 8 | Task state is indeterminate |

## Authority boundary

The CLI does not grant `fleet.hermes.run`. Execution requires a unique schema-v2 managed-identity policy entry that explicitly permits the operation, current managed state, an authenticated generation-consistent binding, and current readiness/capacity evidence.

`fleet doctor` is read-only. It reports service/configuration/readiness findings and never restarts, disables, deletes, or repairs host state.
