# Fleet Operator Foundation

Fleet's operator foundation is the presentation-neutral application boundary for future CLI and Hermes Desktop operator surfaces. It composes existing owners rather than introducing another registry, readiness model, scheduler, transport, task ledger, or execution runtime.

```text
future CLI / Desktop adapter
  -> hermes_fleet.operator.OperatorService
       -> Fleet managed-control API
       -> canonical local operator policy
       -> existing exact-node controller submission
       -> public Keryx task APIs
```

The operator layer contains typed node, readiness, resolved-target, submission/completion, error, and diagnostic results. It contains no terminal rendering, Desktop components, shell-based Fleet invocation, direct Fleet/Keryx database access, or repair actions.

## Canonical active configuration

The active operator configuration is the existing profile-scoped file:

```text
$HERMES_HOME/fleet/nodes.yaml
```

`hermes fleet init` creates schema version 2 for new installations. Schema version 1 remains readable for the existing static exact-peer controller surfaces. Schema version 2 adds `managed_targets`, which holds explicit local policy keyed by authoritative managed identity:

```yaml
schema_version: 2
defaults: {}
nodes: []
managed_targets:
  - source: nodescale
    network_id: example-network
    device_id: example-device
    target_name: worker
    policy:
      allowed_operations:
        - fleet.hermes.run
```

`target_name` preserves the existing exact worker envelope identity expected by the proven execution substrate; it is not a network locator. This file is credential-free. Tokens, private keys, API credentials, transport endpoints, and service secrets remain outside it.

The state split is deliberate:

- Fleet managed control owns current managed identity, binding generation and authenticated Keryx peer provenance, readiness, freshness, capacity, and managed baseline operations.
- `nodes.yaml` owns explicit local operator policy and resource limits.

Managed projection cannot generate `fleet.hermes.run`. A managed node without an exact `managed_targets` policy entry remains denied for execution. The operator layer never converts membership, managed state, readiness, or advertised operations into execution authority.

## Target resolution

Operator targets resolve deterministically against the authoritative managed overview. Supported human-facing selectors are:

- an explicitly configured Fleet alias;
- the managed `device_id`;
- the opaque Fleet `stable_id`.

Resolution then re-inspects the exact managed projection through the local control API and obtains the current `authenticated_peer_id` from binding provenance. The binding generation must still match the overview row. Unknown and ambiguous selectors are rejected. Missing or changed binding state fails closed. Hostnames, addresses, tags, and peer IDs are not inferred as operator authority.

The resulting peer ID remains diagnostic and routing data. It is not the primary operator identifier and is not stored in the schema-v2 managed policy entry.

## Supported application operations

The initial reusable service supports:

- listing authoritative managed nodes;
- inspecting one node and its readiness;
- resolving one exact managed target;
- deliberate exact-target `fleet.hermes.run` submission and completion;
- public Keryx task reattachment where the pinned SDK supports it.

Existing direct-operation and static schema-v1 controller behavior remains unchanged. Future adapters can migrate those surfaces onto the same operator models without making the CLI the application architecture.

## Structured failures

`OperatorErrorCode` provides bounded categories including unknown/ambiguous target, unmanaged or stale state, missing binding, readiness/capacity failure, policy denial, unavailable operation/transport/Hermes, deadline, failed task, and indeterminate task. Operator messages are sanitized while the exception retains separate debugging detail for logs.

## Read-only diagnostics

`OperatorDoctor` returns structured findings without changing the host. It checks the relevant Fleet, managed-control, Keryx, and Hermes services; missing active configuration; missing explicit execution policy; stale readiness; repeated gateway restarts; and multiple active Hermes gateway services targeting the same profile.

Doctor never stops, restarts, disables, deletes, or rewrites services or configuration.

## Current gaps

- The managed control overview exposes authoritative readiness and effective managed operations. Exact binding peer provenance requires a second exact projection inspection; the operator service performs that generation-fenced read rather than reconstructing state.
- Task reattachment can expose only bounded fields available from the public Keryx task record. It does not read Fleet execution-binding SQLite or duplicate Keryx records.
- Installed revision comparison is reported only where reliable bundle metadata exists. The operator diagnostic does not guess source commits from process health or package versions.
- Phase 1 does not add CLI commands or Desktop execution UI.
