# Unified Setup V1

`fleet setup` is the first presentation-neutral convergence surface for the
Fleet stack. It composes existing owners and reports structured state; it does
not create Nodescale trust, Keryx identity, Fleet managed state, or execution
policy.

## Commands

```text
fleet setup [--bundle PATH] [--json]
fleet node adopt SSH_TARGET --bundle PATH [--json]
```

`fleet setup` is read-only when no bundle is supplied. It checks controller
prerequisites and returns stable structured checks. Supplying a bundle adds
exact-bundle availability to the check; it does not silently mutate authority.

`fleet node adopt` resolves the target against the local Tailscale provider
observation, requires exactly one online stable provider identity, verifies
non-interactive SSH, captures an installer-owned rollback snapshot, transfers
an exact worker bundle, then delegates doctor/install to
`hermes-fleet-node`. It never invokes the Nodescale owner/adoption/trust tools
and therefore reports `trusted=false`, `managed=false`, and
`execution_authorized=false` until those independent owner-controlled stages
are completed.

The current V1 worker convergence requires `hermes-fleet-node` to already be
available on the target. A completely bare host must first receive the
versioned Fleet bootstrap artifact. This is an explicit blocker, not a reason
to fall back to a developer checkout, direct database writes, or fabricated
identity.

## Idempotency and rollback

The worker installer verifies every bundle hash, preserves an existing Keryx
daemon credential, rejects inconsistent credentials before mutation, records
a private rollback snapshot of files it owns, and only restarts changed
services in dependency order. Re-running the same accepted bundle does not
rotate identity or regenerate credentials.

## Authority boundary

Setup may install software and service definitions. The following remain
separate explicit facts owned elsewhere:

1. provider observation and stable device selection;
2. owner-authorized Nodescale adoption and DeviceId issuance;
3. owner-authorized trust activation;
4. authenticated Keryx binding;
5. Fleet managed projection and readiness;
6. explicit `fleet.hermes.run` policy.

Network reachability and successful software installation grant none of these.
