# Fleet Integration Verification

This guide defines a repeatable two-node verification procedure for Hermes Fleet. It is intended for any controller/worker deployment and deliberately avoids environment-specific task IDs, hostnames, account names, or historical release evidence.

Run verification against the exact Fleet and Keryx revisions being evaluated.

## Test topology

```text
controller Fleet
  -> controller keryxd / keryx-node
  -> authenticated Keryx route
  -> worker keryx-node / keryxd
  -> fleet-node
       -> direct handlers
       -> local Hermes Runs API for fleet.hermes.run
```

## Preconditions

Before testing:

- the relay and registry endpoints are healthy;
- both local Keryx daemons report ready;
- participating edge nodes are authenticated and connected;
- the worker advertises the expected Fleet skills;
- the worker's local Hermes Runs API is healthy if execution testing is enabled;
- Fleet operator inventory resolves the worker name to the intended Keryx peer ID;
- Fleet execution-binding state is writable only by the intended service account;
- no unsupported bridge or direct-database fallback is handling Fleet tasks.

Record the Fleet and Keryx Git revisions used for the test.

## 1. Direct communication

From the controller, send a bounded message:

```bash
hermes fleet message WORKER "FLEET_MESSAGE_OK" \
  --topic smoke-test \
  --correlation-id fleet-message-smoke
```

Verify:

- the request succeeds;
- the operation is `fleet.message`;
- the acknowledgement is marked untrusted;
- Keryx reports a real task ID, routed peer, and delivery route;
- the routed peer is the exact peer configured for `WORKER`;
- worker logs correlate the same task and authenticated sender;
- no Hermes run is created;
- no Fleet execution-binding row is created for the direct message.

Then reattach by task ID:

```bash
hermes fleet status TASK_ID
```

The reopened status must come from durable Keryx state and must not invent a new submission receipt.

## 2. Health and inventory

Run:

```bash
hermes fleet health WORKER
hermes fleet inventory WORKER
```

Verify:

- responses correspond to the selected worker;
- peer-produced response data is marked untrusted;
- neither operation creates a Hermes run;
- response size and timing remain within configured bounds;
- malformed or unauthorized requests fail closed.

## 3. Deliberate Hermes execution

If policy permits `fleet.hermes.run`, execute a deterministic request such as:

```bash
hermes fleet run WORKER "Return exactly FLEET_OK"
```

Verify:

- the request succeeds only when explicitly authorized;
- the Keryx task is routed to the intended worker;
- the worker creates exactly one Hermes run for that task;
- Fleet persists a task-to-run binding;
- the terminal text is `FLEET_OK` for this deterministic test;
- peer/model output is marked untrusted;
- the binding reaches a terminal completed state;
- durable status reattachment returns the same terminal result.

Re-run the worker handling path for the same claimed Keryx task in a controlled test. It must resume/replay the known binding rather than create a second Hermes run.

## 4. Deadline behavior

### Direct health deadline

Use a short allowed deadline and verify:

- the absolute Keryx deadline is the source of truth;
- downstream health probes share the remaining budget;
- the remaining budget is recomputed between probes;
- an already-expired request performs no work;
- Fleet does not report success after the deadline;
- no health request creates a Hermes run.

### Executable deadline

Submit deliberately slow executable work with a short deadline and verify:

- Hermes polling stops when the budget is exhausted;
- Fleet attempts bounded stop/cleanup when the local Runs API is reachable;
- the Keryx task does not report successful completion after expiry;
- task reclaim does not create a second Hermes run.

## 5. Authorization failures

Verify default-deny behavior with controlled requests:

- an unapproved sender cannot invoke an operation;
- an operation denied by local policy is rejected;
- a managed baseline grant cannot authorize `fleet.hermes.run`;
- local operator deny overrides a generated managed grant;
- envelope/metadata disagreement is rejected;
- wrong destination identity is rejected;
- unsupported envelope versions are rejected.

## 6. Trust-boundary checks

For every peer-returned operation:

- authenticated sender identity comes from Keryx, not from the Fleet JSON body;
- remote content is presented as untrusted;
- remote fields cannot replace the selected target, task ID, routed peer, delivery route, or local authorization decision;
- malformed peer output does not become controller configuration or authority.

## 7. Managed projection

When Nodescale integration is enabled, verify separately on the worker host:

- the projection service accepts only the configured peer UID;
- projection uses the local Unix-domain socket rather than the network;
- valid successor generations apply durably;
- exact replay is idempotent;
- conflicting, stale, and skipped generations fail with the defined outcomes;
- `inspect` survives service restart and returns authoritative durable state;
- generated operations are limited to health, inventory, and message;
- `fleet.hermes.run` cannot be generated;
- local operator deny remains effective.

See [Managed projection V1](managed-projection-v1.md).

## 8. Cancellation behavior

Do not treat origin-side cancellation as proof that a remote Hermes run stopped.

Until the end-to-end cancellation contract can prove remote observation and bound-run termination, cross-node running-task cancellation should return an explicit unavailable/fail-closed result.

## Evidence to retain

For a release or deployment qualification, retain environment-appropriate evidence outside durable repository documentation:

- exact Fleet and Keryx revisions;
- CI run links for those revisions;
- service status and relevant log windows;
- request/result JSON with secrets redacted;
- correlated Keryx task and Hermes run IDs;
- bounded Fleet binding-state rows;
- confirmation that direct operations created no Hermes runs;
- confirmation that unsupported fallback services did not run.

Never retain node tokens, API keys, private keys, TLS private material, or model credentials in a public evidence bundle.

## Repository gates

At minimum, run the repository's normal Python and Rust checks before declaring a revision qualified:

```bash
python -m pytest
python -m ruff check .

cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --workspace
```

Historical successful smoke tests are useful release history but are not evidence that a changed revision still satisfies these contracts.
