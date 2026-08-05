# Changelog

## 0.1.0 - 2026-08-05

### Added

- Keryx-first Phase 1 local Fleet domain package and standalone Hermes plugin scaffold.
- Strict schema-v1 friendly-name to immutable Keryx peer-ID inventory, per-node policy, envelopes, deterministic selection, and owner-safe local initialization.
- Public `hermes fleet init` command and stable-shape, non-networking `fleet_list_nodes` placeholder that reports `FEATURE_NOT_IMPLEMENTED` rather than an ambiguous empty success.
- Strict JSON envelope decoding that rejects duplicate object members at every depth, Python-only non-finite constants, and decoded lone surrogates without echoing payload contents.
- Bounded `fleet.message` envelopes and one explicit fleet-node dispatcher separating direct health/inventory/message handling from deliberate `fleet.hermes.run` execution.
- Direct Keryx controller submission preserving actual routed-peer/route receipts, authenticated loopback Hermes Runs start/poll/stop support, and a narrow durable task-to-run binding that prevents duplicate Hermes execution on reclaim.
- Live Keryx node inventory, high-level exact-node health/inventory/message/run views, and durable task-status reattachment.
- Seven async Hermes tools, the bounded `hermes fleet` CLI tree, the plugin-root operator skill, and foreground `fleet-node` service entry point.
- Systemd unit files plus deployment and two-machine smoke-test runbooks using the VPS `admin` Hermes profile.

### Security and scope

- Default-deny operation policy, bounded request fields, strict envelope validation, safe export paths, and explicit untrusted remote-output presentation.
- Phase 1 shipped without Keryx SDK/network integration, direct Hermes A2A, discovery, dispatch, credential handling, history/task database, daemon, or dashboard; later functional slices preserve the no-duplicate-transport/database boundary.
- Artifact documentation distinguishes current descriptor/text-preview behavior from deferred authenticated artifact-byte transport and Python retrieval work.
- The executable plan freezes generalized hardening after Phase 1 and requires both one direct Katana-to-VPS message acknowledgment and one deliberate remote Hermes execution before the first functional release; pub/sub, inboxes, Kanban integration, artifacts, fan-out, and Android remain backlog.
- Cross-node cancellation remains fail-closed until Keryx can prove destination-worker interruption; origin-only cancellation is not exposed as success.
