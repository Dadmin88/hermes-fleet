# Changelog

## 0.1.0 - 2026-08-05

### Added

- Keryx-first Phase 1 local Fleet domain package and standalone Hermes plugin scaffold.
- Strict schema-v1 friendly-name to immutable Keryx peer-ID inventory, per-node policy, envelopes, deterministic selection, and owner-safe local initialization.
- Public `hermes fleet init` command and stable-shape, non-networking `fleet_list_nodes` placeholder.

### Security and scope

- Default-deny operation policy, bounded request fields, strict envelope validation, safe export paths, and explicit untrusted remote-output presentation.
- No Keryx SDK, network transport, direct Hermes A2A, discovery, dispatch, credential handling, history/task database, daemon, or dashboard.
