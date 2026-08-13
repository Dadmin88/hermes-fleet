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
non-interactive SSH and unprivileged Python/systemd prerequisites, transfers
an exact verified worker bundle, stages the bundled Fleet bootstrap helper,
captures an installer-owned rollback snapshot, then delegates doctor/install to
`hermes-fleet-node`. It never invokes the Nodescale owner/adoption/trust tools
and therefore reports `trusted=false`, `managed=false`, and
`execution_authorized=false` until those independent owner-controlled stages
are completed.

The target does not need a preinstalled Fleet helper, Hermes runtime, or Fleet
systemd units. The verified bundle supplies exact Fleet/Keryx wheels and
binaries, a Git-archived pinned Hermes source tree, and canonical user units
for Keryx, the loopback Hermes Runs API, and Fleet. The target must already
provide Python 3.11+ with `venv`/`ensurepip`, systemd user services, Tailscale,
login persistence (`loginctl ... Linger=yes`), and the selected OCI runtime;
missing host-level prerequisites fail before setup mutation. Enabling linger
is an explicit owner/administrator action; setup detects but does not escalate
privileges to enable it.

## Idempotency and rollback

The worker installer verifies every bundle hash, preserves existing scoped
credentials, rejects inconsistent credentials before mutation, records a
private rollback snapshot of files it owns, and restores owned files, the
isolated `fleet-worker` profile, and service enable/active state after a
post-mutation failure. Re-running the same accepted bundle does not rotate
identity, regenerate credentials, or restart unchanged services.

The dedicated `fleet-worker` profile is created empty without cloning the
operator's default profile or model credentials. The Keryx daemon token is
visible only to Keryx/Fleet consumers; the loopback Runs API key is visible
only to Hermes/Fleet consumers. Model/provider credentials remain a separate,
explicit scoped provisioning step.

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
