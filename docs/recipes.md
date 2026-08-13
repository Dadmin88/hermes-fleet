# Fleet Recipe contracts

Fleet Recipe contracts separate logical execution requirements from immutable resolution and later backend realization.

```text
FleetRecipe → ResolvedRecipe → future ExecutionPlan → future ExecutionBackend
```

Only the first two contracts are implemented here. They do not execute work, select a node, choose a backend, install a profile, grant authority, or persist runtime state.

## `fleet.recipe.v1`

`FleetRecipe` expresses runtime-neutral requirements:

- one Agency profile requirement by logical name and requested version expression;
- accepted operating systems and architectures;
- minimum CPU millicores and memory bytes;
- named isolation and network requirements;
- bounded namespaced extension data.

It deliberately has no backend, Docker/OCI, host path, peer, node, scheduler, workflow, Keryx, or execution-plan field.

## `fleet.resolved-recipe.v1`

`ResolvedRecipe` binds a Recipe content hash to exact immutable ingredients:

- approved Agency repository identity;
- exact full git object ID;
- exact profile name and version;
- independently verified `hermes-agency-profile-content.v1` SHA-256 digest;
- bounded namespaced resolver extension data.

A ResolvedRecipe still has no node selection or backend realization. Those belong to later contracts.

## Canonical identity

Both contracts use deterministic canonical JSON with sorted object keys and compact separators. Their `content_hash` is `sha256:<hex>` over those canonical bytes. Equivalent object ordering therefore produces the same identity.

Unknown extension data is preserved only under reverse-domain-style namespaced keys. Preservation does not mean Fleet understands, authorizes, or executes it. Values are bounded JSON-compatible data; floating-point values are excluded to avoid cross-runtime canonicalization ambiguity.

## Example

```json
{
  "schema": "fleet.recipe.v1",
  "agent": {
    "kind": "agency_profile",
    "name": "researcher",
    "version": ">=1,<2"
  },
  "environment": {
    "os": ["linux"],
    "architecture": ["x86_64", "aarch64"]
  },
  "resources": {
    "cpu_millis": 500,
    "memory_bytes": 536870912
  },
  "security": {
    "isolation": "process",
    "network": "restricted"
  },
  "extensions": {}
}
```

## Current limitation

These are logical schema/value contracts in the Python compatibility layer. They are not yet carried in `fleet.hermes.run`, persisted by `fleet-state`, resolved automatically from Agency, mapped to a backend, admitted by a worker, or scheduled. Current native exact execution remains unchanged.
