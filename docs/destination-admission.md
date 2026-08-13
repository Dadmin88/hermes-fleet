# Destination admission contract

FX7 defines the pure, provider-neutral admission decision that must succeed before FX8 may realize and submit an exact-node Recipe execution.

Admission consumes an already-resolved exact destination. It does not discover candidates, rank nodes, reserve capacity, create trust, or submit work.

## Required current evidence

A positive `AdmissionDecision` binds:

- the durable execution instance ID;
- exact Recipe and capability snapshot hashes;
- stable Nodescale-managed source/network/device identity;
- current binding and admission generations;
- the single `fleet.hermes.run` operation;
- evaluation time before the request deadline.

The caller must derive `DestinationAdmissionContext` from authoritative current state immediately before execution:

- the projection is active and exact generations still match;
- the Keryx binding is authenticated;
- local Fleet policy explicitly authorizes `fleet.hermes.run`;
- readiness evidence is current and scheduler-ready;
- at least one worker slot is presently available;
- the backend capability snapshot still matches the resolved Recipe.

Any unavailable or contradictory evidence fails closed with a typed status. A positive decision carries the ingredient hashes and operation so downstream code does not need to trust mutable ambient inputs.

## TOCTOU boundary

This contract is a pure decision over a single coherent snapshot. It does not claim a cross-store transaction across Nodescale, Fleet, Keryx, and the backend. FX8 must evaluate it as late as possible, then pass the exact decision and generation-fenced instance forward. Destination admission is not a durable capacity reservation; automatic placement and reservations remain deferred to FX9.

## Not implemented here

- no scheduler or target selection;
- no capacity lease;
- no backend realization;
- no Keryx task submission;
- no trust or execution grant creation;
- no second task or artifact ledger.
