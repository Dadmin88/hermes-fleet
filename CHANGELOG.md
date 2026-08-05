# Changelog

## 0.1.0 - 2026-08-05

### Added

- Keryx-first Phase 1 local Fleet domain package and standalone Hermes plugin scaffold.
- Strict schema-v1 friendly-name to immutable Keryx peer-ID inventory, per-node policy, envelopes, deterministic selection, and owner-safe local initialization.
- Public `hermes fleet init` command and stable-shape, non-networking `fleet_list_nodes` placeholder that reports `FEATURE_NOT_IMPLEMENTED` rather than an ambiguous empty success.
- Strict JSON envelope decoding that rejects duplicate object members at every depth, Python-only non-finite constants, and decoded lone surrogates without echoing payload contents.

### Security and scope

- Default-deny operation policy, bounded request fields, strict envelope validation, safe export paths, and explicit untrusted remote-output presentation.
- No Keryx SDK, network transport, direct Hermes A2A, discovery, dispatch, credential handling, history/task database, daemon, or dashboard.
- Artifact documentation distinguishes current descriptor/text-preview behavior from deferred authenticated artifact-byte transport and Python retrieval work.
- The executable plan freezes generalized hardening after Phase 1 and makes one-controller/one-worker Katana-to-VPS text execution the first release target; artifacts, fan-out, and Android remain backlog.
