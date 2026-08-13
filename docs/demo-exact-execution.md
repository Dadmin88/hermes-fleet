# Fleet Desktop exact-execution demo

This runbook demonstrates the real Fleet operator path without demo-only runtime behavior.

## What the demo proves

The demo shows a managed node selected by stable Fleet identity, an explicitly authorized exact-target Hermes task submitted through the shared operator service, durable Keryx task identity, terminal Hermes output, and a truthful Desktop timeline.

It does not prove automatic placement, Recipe execution, workflow execution, broad node authorization, or successful cancellation of already-running remote work.

## Prerequisites

- Hermes Desktop and gateway are using the same active profile.
- Hermes Fleet is installed as both the Hermes backend plugin and Desktop runtime plugin.
- `fleet-managed-control` is running for that profile.
- The intended worker is visible as managed, active, fresh, and scheduler-ready.
- The target's schema-v2 managed policy explicitly allows `fleet.hermes.run`.
- The controller has authenticated Keryx configuration.
- The worker has an authenticated current Keryx binding and a reachable authenticated Hermes Runs API.

Do not grant execution merely for the recording. If the target is not already authorized under the intended operator policy, record the graph/readiness portion only.

## Preflight

Run the CLI checks first. Replace `<target>` with the same stable managed identity shown in Desktop.

```bash
fleet node show <target> --json
fleet readiness <target> --json
fleet doctor --json
```

Confirm that readiness is current and `fleet.hermes.run` is explicitly present. Do not continue if identity resolution is ambiguous, the binding is stale, readiness is false, or policy is absent.

## Recording sequence

1. Open **Fleet → Overview** and pause on the real summary.
2. Open **Network** and fit the graph so the controller and intended worker are visible.
3. Select the worker and open its Inspector.
4. Point out stable identity, managed state, binding generation, readiness, capacity, and advertised operations.
5. Enter a bounded, non-sensitive prompt such as:

   ```text
   Return exactly this non-identifying confirmation: "The bounded demo task completed on the selected worker." Do not include credentials, paths, network addresses, tokens, profile names, hostnames, or other machine identifiers.
   ```

6. Select **Run on exact node** once.
7. Keep the Inspector open while the real task state refreshes. Never click Run again to recover from uncertain status; polling reattaches to the returned durable task identity.
8. When terminal, show the result and the final completion category. If status is indeterminate, present it as indeterminate rather than retrying.
9. Return to the graph and show that node authority/readiness did not change merely because a task completed.

## Operator narration

> Fleet is showing current managed state, not a static diagram. The selected label is only a human selector; execution resolves the stable managed identity to its current authenticated Keryx binding at operation time. Fleet then rechecks explicit execution policy and readiness before creating one durable task. Desktop and CLI use the same operator application service. The timeline only marks facts this request path can prove, and status refresh reattaches to the existing task instead of submitting another run.

## Capture framing

Capture these four frames:

1. **Fleet exists** — Overview with managed, ready, observed, and attention summaries.
2. **Identity and readiness** — Network graph plus managed-node Inspector.
3. **Durable execution** — submitted task identity and pending completion.
4. **Terminal truth** — completed, failed, timed-out, or indeterminate state with bounded result when available.

Crop private topology and avoid showing peer IDs, provider node IDs, IP addresses, local paths, tokens, task IDs from non-demo runs, or unrelated Desktop content.

## Deterministic fallback

If no worker is safely authorized or ready, demonstrate only Overview, Network, Inspector, and disabled execution. Do not seed fake production nodes and do not loosen policy to finish the recording. Isolated frontend fixtures remain test-only evidence.

## Reset and cleanup

- No product state should need reset after a successful task.
- Close the Inspector to discard its renderer-local prompt and task display.
- Confirm the task reached a truthful terminal or indeterminate state in `fleet task show <task-id> --json` if follow-up is needed.
- Preserve private acceptance evidence outside the public repository.
