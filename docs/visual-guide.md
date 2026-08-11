# Hermes Fleet visual guide

This page collects the canonical dark-mode architecture visuals for Hermes Fleet. The SVG sources live under [`docs/assets/`](assets/) so they remain sharp at any size and easy to maintain alongside the contracts they explain.

## Ecosystem overview

The ecosystem view shows where Fleet sits between private connectivity, device trust, authenticated transport, the Fleet worker, Hermes Agent, Hermes Agency, and operator surfaces.

![Hermes Fleet ecosystem](assets/hermes-fleet-ecosystem.svg)

Use this visual with the [ecosystem map](ecosystem.md) and [architecture guide](architecture.md).

## Request lifecycle

This view explains the exact-node request path and the important split between direct Fleet operations and the one current executable operation, `fleet.hermes.run`.

![Hermes Fleet request lifecycle](assets/hermes-fleet-request-lifecycle.svg)

Use this visual with the [architecture guide](architecture.md).

## Profile identity and execution locality

This view separates current exact native-profile evidence from the planned Recipe-based execution fabric. Current Fleet can validate exact Agency package identity and observe native locality. Planned Recipe execution moves environment requirements into `Fleet Recipe -> ResolvedRecipe -> ExecutionPlan -> scheduler -> worker materialization` rather than making persistent remote host-profile installation the default placement mechanism.

![Profile identity and execution locality](assets/profile-identity-and-execution-locality.svg)

Use this visual with [profile identity, presence, and execution locality](profile-placement.md).

## Node readiness and exact profile eligibility

Readiness and exact package presence answer different questions. A node can be scheduler-ready without carrying the requested native package, and it can carry the exact package while being stale or otherwise not ready.

![Node readiness and exact profile eligibility](assets/node-readiness-and-profile-eligibility.svg)

Use this visual with [node observations and scheduler readiness](node-readiness.md).

## Asset policy

These files are the canonical visual sources for the Fleet architecture. Other Hermes repositories should link to these assets or to this guide when they need cross-repository context rather than maintaining divergent copies of the same diagrams.
