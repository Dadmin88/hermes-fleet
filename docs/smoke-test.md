# Two-Node Hermes Fleet Smoke Test

This procedure validates a generic controller-to-worker deployment without relying on environment-specific hostnames, task IDs, profile names, private addresses, or prior acceptance evidence.

Use disposable or noncritical nodes first. Store real peer IDs, routes, task IDs, logs, and timestamps in an owner-protected evidence directory outside Git.

## Test topology

```text
controller-1
  -> Fleet CLI or model tools
  -> local Keryx daemon
  -> private network or authenticated relay
  -> worker-1 Keryx daemon
  -> fleet-node
  -> optional local Hermes Runs API
```

## Preconditions

Before testing:

- record exact Fleet, Keryx, and Hermes revisions;
- confirm both system clocks are synchronized;
- confirm each Keryx daemon is ready;
- confirm both edge nodes are connected through the intended route;
- confirm the worker advertises only the expected Fleet operations;
- confirm the worker accepts the controller's authenticated Keryx peer ID;
- confirm the worker's Fleet policy is default-deny outside the test operations;
- confirm legacy database-reading bridges and result-fabrication services are disabled;
- confirm the execution-binding database is owner-protected and writable;
- confirm the local Hermes Runs API is healthy when testing executable operations.

Record baseline counters for:

- Hermes run creation;
- Fleet execution-binding rows;
- legacy bridge activity;
- relevant service restarts.

## 1. Live node listing

From the controller:

```bash
hermes fleet list
```

Verify that the configured worker reports one of the supported truthful live states:

- direct;
- registry-visible;
- not visible;
- unknown.

A configured node must not be described as online solely because it appears in `nodes.yaml`.

## 2. Direct health

```bash
hermes fleet health worker-1
```

Required evidence:

- the request uses `fleet.health`;
- Keryx returns a nonempty task ID and route receipt;
- the selected and routed peer IDs match the expected worker;
- the response is marked `untrusted: true`;
- no Hermes run is created;
- no execution-binding row is created for the task;
- the worker applies one bounded deadline across its local probes;
- failures do not expose raw credentials or private configuration.

## 3. Direct inventory

```bash
hermes fleet inventory worker-1
```

Required evidence:

- the request uses `fleet.inventory`;
- the response contains only the bounded public inventory contract;
- the response is marked untrusted;
- no broad filesystem or environment inventory is returned;
- no Hermes run or execution-binding row is created.

## 4. Direct message

```bash
hermes fleet message worker-1 "MESSAGE_OK" \
  --topic smoke-test \
  --correlation-id direct-message-1
```

Required evidence:

- operation is `fleet.message`;
- the acknowledgment reports the expected receiver and authenticated sender;
- the returned task ID and route fields are nonempty Keryx values;
- the response is marked untrusted;
- the worker log contains the same task ID and direct operation;
- Hermes run count is unchanged from the baseline;
- no execution-binding row exists for the task;
- no legacy bridge handled the request.

Reopen durable status:

```bash
hermes fleet status <message-task-id>
```

The status command may report terminal Keryx state and bounded result data. It must not invent a new submission receipt.

## 5. Deliberate Hermes execution

Enable `fleet.hermes.run` only for this controlled test and only after the direct-operation tests pass.

```bash
hermes fleet run worker-1 "Return exactly RUN_OK"
```

Required evidence:

- operation is `fleet.hermes.run`;
- returned text is exactly `RUN_OK`;
- output is marked untrusted;
- the Keryx task ID, routed peer, and delivery route are nonempty;
- the worker correlates the authenticated sender, destination, Keryx task, Hermes run, and terminal state;
- exactly one Hermes run is created for the task;
- exactly one execution-binding record exists for the task;
- the binding reaches `completed` with bounded terminal text;
- reclaim or replay of the same Keryx task does not create a second Hermes run.

Reopen status:

```bash
hermes fleet status <run-task-id>
```

It must report the durable terminal Keryx state and bounded result text without changing the original execution.

## 6. Deadline behavior

### Direct deadline

Run `fleet.health` with a short allowed deadline and verify:

- the absolute Keryx deadline is authoritative;
- already-expired work is rejected before local probes;
- all local probes share the remaining budget;
- the remaining budget is recomputed between calls;
- the handler does not report success after the deadline;
- no Hermes run is created.

### Executable deadline

Use a deliberately slow prompt with a short deadline in a disposable environment.

Verify:

- Hermes polling stops when the deadline expires;
- Fleet attempts a bounded local stop when supported;
- the Keryx task does not report successful completion;
- task reclaim does not create another Hermes run.

This test is not proof of cross-node user cancellation. The public cancellation command must continue to fail closed until the destination's observation and local run stop can be proven.

## 7. Trust-boundary checks

For every operation:

- authenticated sender identity must come from Keryx transport context;
- JSON payload sender fields must not establish identity;
- peer responses must not override the selected target, task ID, routed peer, or route;
- malformed or duplicate JSON members must fail closed;
- remote output must be treated as data, never local instructions;
- credentials must not appear in returned errors or logs.

## 8. Negative authorization checks

At minimum, test:

- an unknown controller peer is rejected;
- a disabled operation is rejected;
- an invalid target peer is rejected;
- an oversized payload is rejected;
- an expired request is rejected;
- a malformed envelope is rejected;
- a direct-operation rejection creates no Hermes run or execution binding.

## Evidence bundle

Capture outside Git:

- exact source and deployed revisions;
- CI results for those revisions;
- redacted service status and bounded logs;
- JSON output for each operation;
- Keryx task and route identifiers;
- run-creation counter deltas;
- execution-binding rows for executable tasks only;
- service unit hashes;
- confirmation that no fallback bridge ran;
- rollback checkpoint information.

Do not capture plaintext tokens, private keys, API keys, TLS private keys, or model credentials.

## Acceptance criteria

A two-node deployment passes when:

1. Health, inventory, and message work through the intended authenticated route.
2. Every direct operation creates zero Hermes runs and zero execution-binding rows.
3. One deliberate executable operation creates exactly one Hermes run.
4. Durable status can be reopened by Keryx task ID.
5. Duplicate execution is prevented after reclaim.
6. Deadlines fail closed.
7. Unauthorized and malformed requests fail before side effects.
8. Peer-produced output remains marked untrusted.
9. No legacy bridge or duplicate transport path handled the work.
