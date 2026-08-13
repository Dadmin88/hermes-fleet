# Backend capability contracts

`BackendCapabilities` describes what one execution backend can guarantee. It is provider-neutral input to hard eligibility checks, not placement or scheduling.

```text
FleetRecipe + BackendCapabilities → CapabilityMatch
```

## Contract

`fleet.backend-capabilities.v1` describes:

- backend kind as a namespaced identifier;
- operating system and architecture;
- supported isolation and network guarantees;
- hard CPU and memory capacity;
- filesystem guarantees;
- Agency-profile and artifact materialization support;
- bounded namespaced extension data.

A capability document does not contain a node, peer, hostname, command, image, container, socket, credential, or runtime configuration.

## Eligibility only

`evaluate_capabilities()` compares a logical `FleetRecipe` with one capability document and returns:

- `eligible`: whether every hard requirement is satisfied;
- `reasons`: a deterministic set of incompatibility codes.

The evaluator does not rank candidates, select a node, reserve capacity, authorize execution, or submit work. Those remain later explicit concerns.

## Isolation and provider neutrality

Isolation and network values are declared guarantees. An implementation may eventually map them to OCI, a native sandbox, a VM, or another mature runtime. This contract does not encode Docker commands or assume Docker is the only backend.
