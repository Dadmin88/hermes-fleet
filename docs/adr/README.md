# Hermes Fleet Architecture Decision Records

Architecture Decision Records (ADRs) provide a lightweight public record for major Fleet architecture choices made from this point forward.

The ADR directory is intentionally prospective. Decisions that predate this convention are **not** retroactively backdated or rewritten to look as though an ADR existed earlier. Historical provenance remains anchored to the original issues, commits, pull requests, documentation, and acceptance evidence listed in [`../provenance.md`](../provenance.md).

## When to write an ADR

Use an ADR when a change establishes or materially changes a durable architecture boundary, especially when it affects:

- authority or trust ownership;
- machine-boundary semantics;
- transport versus execution responsibilities;
- identity or idempotency rules;
- scheduling, placement, or admission policy;
- persistent Agent Instance / RunAuthority / Run Capsule semantics;
- disposable execution ownership or isolation;
- evidence, finalization, quiescence, or success semantics;
- memory, learning, skill promotion, Templar, or Vault boundaries;
- cross-repository ownership between Fleet, Keryx, Nodescale, Hermes Agent, or Agency.

Routine implementation details, local refactors, bug fixes, and one-off operational changes generally do not need an ADR.

## Numbering and lifecycle

Use monotonically increasing four-digit numbers:

```text
0001-short-decision-name.md
0002-next-decision.md
```

Statuses:

- `Proposed`
- `Accepted`
- `Superseded`
- `Rejected`

Do not edit an old accepted ADR to conceal a later change of direction. Add a new ADR, mark the earlier decision `Superseded`, and link the two records.

## Evidence chain

An accepted ADR should eventually link to the implementation and proof that made the decision real:

```text
ADR
  → issue / phase
  → implementation PR
  → immutable merge commit
  → verification / acceptance evidence
```

Git timestamps are the provenance clock. Do not manually backdate ADRs.

Start new records from [`0000-template.md`](0000-template.md).
