# Two-Machine Fleet Smoke Test

This document records the accepted Katana-to-VPS v0.1 proof and the repeatable procedure for future deployments.

The controller uses Katana's active `katana` profile. The VPS worker and loopback Hermes Runs API use the VPS `admin` profile.

## Accepted evidence

Acceptance was completed against:

- Fleet runtime SHA: `29876e9b2afa0de8b9f2bce4e1edb5671f412438`
- Fleet CI run: `31062104463`, passed
- Keryx SHA: `f4ee645e415600a959ea8062d1143140bd6c2616`

### Slice A - direct communication

- Task: `7e78f4c1-240a-496f-bbf4-2a0a491018d6`
- Operation: `fleet.message`
- Text: `FLEET_MESSAGE_OK`
- Route: `relay`
- VPS acknowledgment: `received`
- Peer response: `untrusted: true`
- Hermes Runs created: `0`
- Binding rows created: `0`

### Slice B - deliberate remote Hermes execution

- Keryx task: `913af216-2866-48e8-8f18-b479df479466`
- Hermes run: `run_b9f345d82c3d45778b14714966922f7e`
- Route: `relay`
- Result: `FLEET_OK`
- Peer output: `untrusted: true`
- Durable binding: `completed`
- Reattached status: `completed`, result `FLEET_OK`

Live `fleet.health`, `fleet.inventory`, `fleet.list`, and durable status retrieval also passed. A one-second live health deadline passed after the shared-deadline correction. Final exact-SHA reviewers found no release blockers.

## Preconditions for a repeat

- VPS relay health and registry gRPC are healthy over TLS.
- Katana and VPS `keryxd` readiness pass.
- Both edge nodes are connected to the same relay.
- The registry shows the VPS peer with exactly:
  - `fleet.health`
  - `fleet.inventory`
  - `fleet.message`
  - `fleet.hermes.run`
- VPS `GET http://127.0.0.1:8642/health` returns healthy.
- `keryx-task-bridge.service` and `keryx-node-refresh.service` are inactive and disabled on Katana.
- The VPS binding database is writable only by its service account.
- Record exact Fleet and Keryx SHAs plus service start timestamps.

## Slice A procedure

From Katana:

```text
hermes fleet message vps "FLEET_MESSAGE_OK" --topic smoke-test --correlation-id fleet-message-smoke
```

Required evidence:

- `success` is true;
- operation is `fleet.message`;
- status is `received`;
- `untrusted` is true because the acknowledgment originated on a peer;
- `received_by` is the real VPS Keryx peer ID;
- `sender_peer_id` is the real Katana Keryx peer ID;
- returned `task_id`, `routed_to`, and `delivery_route` are nonempty Keryx values;
- `routed_to` equals the configured VPS peer ID;
- the VPS fleet-node log contains the same task ID and direct operation;
- the VPS Hermes Runs API log contains no `POST /v1/runs` in the correlation window;
- the VPS run-binding database contains no row for the task.

Reopen status from Katana:

```text
hermes fleet status <slice-a-task-id>
```

It must report durable Keryx terminal status and the acknowledgment without inventing a submission receipt.

## Slice B procedure

From Katana:

```text
hermes fleet run vps "Return exactly FLEET_OK"
```

Required evidence:

- `success` is true;
- operation is `fleet.hermes.run`;
- response text is exactly `FLEET_OK`;
- `untrusted` is true;
- returned `task_id`, `routed_to`, and `delivery_route` are real Keryx values;
- the VPS fleet-node log correlates the Keryx task ID, authenticated Katana sender peer ID, VPS peer ID, Hermes run ID, and terminal status;
- the VPS Hermes Runs API shows exactly one `POST /v1/runs` for the task;
- the binding database has one completed task-to-run mapping;
- a reclaimed handler for the same task replays the stored terminal text and does not create another Hermes run.

Reopen status from Katana:

```text
hermes fleet status <slice-b-task-id>
```

It must return durable completed status and `result_text: FLEET_OK` with the result marked untrusted.

## Health deadline proof

Use `fleet.health` with a short allowed deadline and verify:

- the worker uses the Keryx absolute deadline as the source of truth;
- both Hermes HTTP probes share the same remaining deadline rather than each receiving a fresh full timeout;
- the remaining budget is recomputed between probes;
- an already-expired request performs no health probes;
- `asyncio.wait_for` prevents the Fleet handler from completing after the Fleet deadline;
- the health operation creates no Hermes run;
- failures return bounded output without raw HTTP details or credentials.

The accepted deployment passed a live health request with a one-second deadline.

## Executable deadline proof

Submit a deliberately slow executable request with a short allowed deadline. Required evidence:

- the worker uses the absolute Keryx deadline metadata;
- Hermes polling stops at the bounded deadline;
- the bound run receives a stop request when the Runs API remains reachable;
- the Keryx task does not report successful completion;
- no second Hermes run is created if the task is reclaimed.

Do not claim cross-node user cancellation. `hermes fleet cancel` and `fleet_cancel_task` fail closed until Keryx can prove the destination worker observed cancellation and stopped the bound Hermes run.

## Trust-boundary proof

For `fleet.health`, `fleet.inventory`, `fleet.message`, and `fleet.hermes.run`:

- all peer-produced response content must be returned with `untrusted: true`;
- authenticated peer identity must come from Keryx, not from the JSON envelope;
- remote fields must not override controller-selected target, task ID, routed peer, or route;
- malformed direct JSON must fail closed.

## Correlated evidence bundle

Capture:

- exact Fleet and Keryx Git SHAs;
- exact Fleet and Keryx CI run URLs;
- Katana daemon and edge-node status/log window;
- VPS relay, daemon, edge-node, Hermes API, and fleet-node status/log window;
- Slice A and Slice B JSON outputs;
- Keryx status reattachment outputs;
- bounded binding rows containing task ID, state, run ID, and no prompt or token;
- service unit hashes or copied unit bytes;
- confirmation that no bridge/fallback service ran.

Redact tokens, API keys, key material, TLS private keys, and model credentials. Peer IDs, task IDs, run IDs, route names, statuses, and timestamps are required correlation evidence and are not credentials.

## Release gate

The v0.1 gate is satisfied when both slices, the health/execution deadline proofs, duplicate prevention, current-byte review, and exact-SHA CI pass on the real machines.

That gate passed for Fleet SHA `29876e9b2afa0de8b9f2bce4e1edb5671f412438` and Keryx SHA `f4ee645e415600a959ea8062d1143140bd6c2616`.
