# Hermes Fleet vNext Phase 13 acceptance: sensitive persistence interception

Status: **CLOSURE GATED**

Phase 13 is complete only when the Hermes Agent prerequisite and the Fleet binding are both merged with green pull-request CI and the resulting `main` commits also have green post-merge CI.

## Master-plan requirements

Phase 13 requires interception before memory, skills, logs, summaries and indexing. The classifier must detect API keys, bearer tokens, passwords, private keys, session cookies, credentials, sensitive environment assignments and sensitive credential files.

Detected material must not become durable reusable context. Logs and evidence may keep only redacted surrounding text; memory, skills, embeddings, search indexing, summaries and promotion candidates must not receive the sensitive body. Classification uncertainty fails closed. Audit records may describe the interception but must not contain the intercepted value.

The master plan also permits the actual value to move to a protected store when policy allows, with only an opaque reference persisted. Phase 13 defines that interception/reference hook but does not create the scoped store or its authorization model. Phase 14 owns that scoped reference implementation. With no Phase 14 policy handler registered, Phase 13 blocks rather than inventing a storage destination.

## One interception boundary

Hermes Agent provides one central persistence-classification layer backed by its existing forced redaction engine. This avoids separate vendor-pattern databases for each sink.

The boundary produces a typed decision containing only:

- sink class;
- allow/redact/block/reference action;
- classification certainty;
- finding classes;
- one-way content fingerprint;
- optional opaque reference.

The audit path never includes the original value.

## Durable sinks

### Native memory

Add, replace and atomic batch operations reject matched values before the memory file or Fleet-scoped metadata can be rewritten. Existing Phase 11 Fleet-specific validation remains authoritative when it has a more specific denial reason.

### Learned skills

Create, edit, patch and supporting-file writes reject matched content before the skill tree is modified. Credential-bearing file names are also treated as sensitive even when the textual body is opaque.

### Transcripts and search indexes

Session message serialization applies interception before SQLite insertion. Structured string leaves such as message content, tool arguments, reasoning sidecars, API content and display metadata are non-reusably redacted for durable transcript storage.

All transcript rewrite/import/compaction paths share the same row serializer. SQLite FTS therefore indexes only the persisted redacted representation rather than the original value.

### Summaries

Compaction treats summary input/output as a blocking boundary for the sensitive body. Surrounding context may continue only after forced non-reusable redaction, and recognizable credential prefixes are removed from durable summaries.

### External memory providers

Provider synchronization is stopped before a matched turn can be sent to an external memory backend. This prevents provider-side embeddings or search indexes from becoming an alternate persistence path.

### Evidence and logs

Pollable run evidence and monitoring exports use the same central boundary. They retain only redacted surrounding context when classification succeeds and fall back to value-free markers when classification is unavailable.

### Promotion candidates

The central policy includes promotion as a blocking sink. There is no active persisted-context promotion engine in the current Phase 13 runtime, so there is no separate promotion write path to wire yet. Future promotion work must call the same boundary and cannot persist the sensitive body.

## Fail-closed behavior

If the classifier cannot execute, blocking sinks refuse persistence and redacting sinks emit only value-free fallback markers. No configuration that disables ordinary live-output redaction disables this persistence boundary.

## Fleet enforcement

Hermes Agent advertises `run_sensitive_interception`. Fleet treats this as a required capability for Phase 12+ context-bound runs. If a Fleet run supplies its context binding to an Agent that does not advertise Phase 13 interception, submission fails before the run is created.

This makes the protection an enforced runtime dependency rather than a best-effort Agent implementation detail.

## Acceptance evidence

Hermes Agent evidence includes focused persistence tests, existing memory/skill compatibility tests, transcript/FTS tests, compaction-boundary tests, external-provider tests, all twelve Python CI slices, platform-specific tests, type-diff, portability checks and post-merge CI. Agent PR #8 merged normally. Its merge commit is `645f3ca1ac3e1da0a35c58775c03127f3b22c461`. The first post-merge CI attempt was transparently cancelled after a macOS runner remained stuck far beyond the corresponding PR job's normal runtime. Attempt 2 on the same exact merge SHA completed successfully, including macOS, Windows, all Python slices, Desktop/JS checks, documentation, packaging and security gates.

Fleet evidence includes exact capability negotiation tests, the existing runtime/memory/context executor suite, the complete Fleet regression suite, pinned-Agent installation smoke, package build/clean-install checks and post-merge CI.

Phase 14 is explicitly out of scope for Phase 13. No scoped protected-store ownership, delegation, expiry or retrieval authorization is implemented here.
