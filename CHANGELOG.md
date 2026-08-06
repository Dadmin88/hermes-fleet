# Changelog

All notable changes to Hermes Fleet are documented here.

The project follows semantic versioning where practical, but pre-1.0 interfaces may change as the Fleet and Keryx contracts mature.

## 0.1.0 - 2026-08-05

Initial experimental release.

### Added

- Keryx-backed communication between configured Hermes-capable nodes.
- Friendly node names mapped to immutable Keryx peer IDs.
- Strict schema-v1 envelopes for:
  - `fleet.health`
  - `fleet.inventory`
  - `fleet.message`
  - `fleet.hermes.run`
- Direct health, inventory, and bounded message handlers that do not create Hermes runs.
- One deliberate remote-execution operation using a local authenticated Hermes Runs API.
- Narrow durable task-to-run binding for duplicate-execution prevention and result replay.
- Live node projection with distinct direct, registry-visible, not-visible, and unknown states.
- Durable Keryx task status and terminal text reattachment by task ID.
- Hermes CLI commands for initialization, listing, health, inventory, messaging, execution, status, and fail-closed cancellation.
- Seven asynchronous Hermes model tools.
- Operator skill, reference systemd units, deployment guide, and repeatable smoke-test procedure.

### Security and correctness

- Default-deny node operation policy.
- Authenticated sender identity sourced from Keryx delivery context rather than request JSON.
- Peer-originated direct responses and Hermes output marked `untrusted: true`.
- Absolute deadline propagation across direct health probes and executable work.
- Fail-closed handling for duplicate JSON members, malformed Unicode, unknown operations or versions, target mismatch, metadata disagreement, expiration, and oversized input.
- Remote approval is never granted automatically.
- Cross-node cancellation returns an explicit unavailable result rather than claiming that a remote run stopped.
- Fleet does not duplicate Keryx transport, task/result storage, or lifecycle state.

### Known limitations

- The release is intended for controlled private-network testing, not production deployment.
- Granular destination-owned per-sender grants are still under development.
- Cross-node cancellation is unavailable.
- Mailbox durability and artifact support depend on future Keryx integration work.
- Scheduling, profile lifecycle, persistent inboxes, workflow graphs, public-internet exposure, and multi-tenant operation are not included.
