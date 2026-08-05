# Two-Machine Fleet Smoke Test

Run this only after the deployment health gate is green. The proof must use Katana's active `katana` profile as controller and the VPS `admin` profile as the loopback Hermes Runs API.

## Preconditions

- VPS relay health and registry gRPC are healthy over TLS.
- Katana and VPS `keryxd` readiness pass.
- Both edge nodes are connected to the same relay.
- The registry shows the VPS peer with exactly:
  - `fleet.health`
  - `fleet.inventory`
  - `fleet.message`
  - `fleet.hermes.run`
- VPS `GET http://127.0.0.1:8642/health` returns healthy.
- `keryx-task-bridge.service` is inactive and disabled on Katana.
- The VPS binding DB is writable only by its service user.

Record the exact Fleet and Keryx Git SHAs and service start timestamps before the first request.

## Slice A — communication without Hermes execution

From Katana:

```text
hermes fleet message vps "FLEET_MESSAGE_OK" --topic smoke-test --correlation-id fleet-message-smoke
```

Required result evidence:

- `success` is true;
- operation is `fleet.message`;
- status is `received`;
- `received_by` is the real VPS Keryx peer ID;
- `sender_peer_id` is the real Katana Keryx peer ID;
- returned `task_id`, `routed_to`, and `delivery_route` are nonempty Keryx values;
- `routed_to` equals the configured VPS peer ID;
- VPS fleet-node log has the same task ID and direct operation;
- VPS Hermes Runs API log has no `POST /v1/runs` for this correlation window;
- the VPS run-binding DB has no row for this task ID.

Then reopen status from Katana:

```text
hermes fleet status <task-id-from-slice-a>
```

It must report the durable Keryx terminal status and acknowledgment without inventing a submission receipt.

## Slice B — deliberate Hermes execution

From Katana:

```text
hermes fleet run vps "Return exactly FLEET_OK"
```

Required result evidence:

- `success` is true;
- operation is `fleet.hermes.run`;
- response text is exactly `FLEET_OK`;
- `untrusted` is true;
- returned `task_id`, `routed_to`, and `delivery_route` are real Keryx values;
- VPS fleet-node log correlates the Keryx task ID, authenticated Katana sender peer ID, VPS peer ID, Hermes run ID, and terminal status;
- VPS Hermes Runs API shows exactly one `POST /v1/runs` for the task;
- the binding DB has one completed task-to-run mapping;
- a repeated/reclaimed handler attempt for the same task replays the stored terminal text and does not create another Hermes run.

Then reopen status from Katana:

```text
hermes fleet status <task-id-from-slice-b>
```

It must return durable completed status and `result_text: FLEET_OK`.

## Deadline proof

Submit a deliberately slow executable request with a short allowed deadline. Required evidence:

- the worker uses the absolute Keryx deadline metadata;
- the Hermes wait stops at the bounded deadline;
- the bound run receives `POST /v1/runs/{run_id}/stop`;
- the Keryx task does not report successful completion;
- no second Hermes run is created if the task is reclaimed.

Do not claim cross-node user cancellation. The first release fails `fleet cancel` closed until Keryx can prove the destination worker observed cancellation and stopped the bound Hermes run.

## Correlated evidence bundle

Capture:

- exact Fleet and Keryx Git SHAs;
- GitHub Actions run URL for the exact Fleet SHA;
- Katana `keryxd` and edge-node status/log window;
- VPS relay, daemon, edge-node, Hermes API, and fleet-node status/log window;
- Slice A and Slice B JSON outputs;
- Keryx status re-open outputs;
- bounded binding rows containing task ID, state, run ID, and no prompt/token;
- service unit hashes or copied unit bytes;
- confirmation that no bridge/fallback service ran.

Redact tokens, API keys, key material, TLS private keys, and model credentials. Peer IDs, task IDs, run IDs, route names, statuses, and timestamps are required correlation evidence and are not credentials.

## Release gate

The first Fleet release is functional only when both slices pass on the real machines, the deadline/duplicate proof passes, current-byte read-only review is clean, and GitHub Actions is green on the exact pushed Fleet SHA.
