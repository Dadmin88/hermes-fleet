# Durable execution instances

> **vNext terminology note:** the historical `ExecutionInstance` described here is a shipped Fleet correlation/recovery record for the earlier exact-destination execution foundation. It is **not** a persistent Hermes **Agent Instance** and it is **not** the vNext temporary **Run Capsule**. The migration/reconciliation of this legacy state belongs to the vNext lifecycle work defined in [vNext foundation](vnext-foundation.md).

FX6 adds Fleet-owned durable correlation for one exact Recipe realization on one exact managed destination. It does not execute work and, for cross-machine execution, it does not replace Keryx task/result/artifact ownership.

## Bound identity

An `ExecutionInstance` permanently binds:

- an operator idempotency key;
- the canonical `ResolvedRecipe` hash;
- the backend-capability snapshot hash;
- stable Nodescale-managed identity;
- the authenticated Keryx binding generation;
- the Fleet admission generation.

Names and aliases are not identity. Generation fencing prevents a later device admission or binding from inheriting an earlier execution instance.

## Lifecycle

```text
reserved
  -> prepared
  -> running
  -> completed | failed | cancelled
  -> cleanup_pending
  -> cleaned
```

Any uncertain provider outcome may enter `indeterminate`. Recovery must inspect authoritative backend and Keryx state before choosing another transition. `cleanup_pending` remains durable until backend cleanup is proven; an unavailable provider is not equivalent to successful cleanup.

Every state change uses an optimistic generation fence. A stale controller cannot overwrite newer state, and terminal/cleaned state cannot regress.

## Ownership boundary

Fleet persists correlation and recovery intent only:

- backend kind and realization identity;
- Keryx task identity;
- lifecycle state and generation;
- exact immutable ingredient and authority references.

For executions that cross a machine boundary, Keryx remains authoritative for durable remote task payloads, terminal results, and artifacts. Backend implementations remain authoritative for runtime realization state. This table is not a second task ledger and must not be generalized into a requirement that same-machine runs traverse Keryx.

FX6 deliberately does **not** implement destination admission, Recipe execution orchestration, placement, reservations, or scheduling. FX7 must validate current destination authority before FX8 is allowed to drive this lifecycle.
