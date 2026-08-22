# ADR-XXXX: Decision title

- **Status:** Proposed
- **Date:** YYYY-MM-DD
- **Owners:** Hermes Fleet maintainers
- **Related:** issue / phase / PR links

## Context

Describe the durable architectural problem being decided. State the existing ownership and trust boundaries that matter.

## Decision

State the decision precisely enough that an implementation and reviewer can tell whether a change conforms to it.

## Invariants

List the rules that must remain true. Prefer explicit statements such as:

- authority can remain equal or narrow but cannot widen;
- transport acknowledgement is not execution success;
- same-machine work does not cross an inter-machine transport boundary;
- ambiguity fails closed.

Delete examples that do not apply.

## Alternatives considered

Record meaningful alternatives and why they were not selected.

## Consequences

Document important benefits, costs, limitations, compatibility effects, and deferred work.

## Security and failure behavior

Explain the relevant threat boundary, recovery behavior, and what happens under uncertainty or partial failure.

## Implementation evidence

Fill these as work lands. Do not invent or backdate evidence.

- Issue / phase:
- Implementation PR:
- Merge commit:
- Verification / acceptance:
- Canonical documentation:

## Supersession

If this ADR supersedes or is superseded by another record, link it here.
