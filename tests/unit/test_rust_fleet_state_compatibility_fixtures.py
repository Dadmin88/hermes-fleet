"""Independent Python-oracle checks for durable Rust fleet-state fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "f0" / "fleet-state-v1.json"


def _expected_state(binding: object) -> dict[str, str]:
    state = getattr(binding, "state")
    expected = {"kind": state}
    run_id = getattr(binding, "run_id")
    result = getattr(binding, "result_text")
    if run_id is not None:
        expected["run_id"] = run_id
    if result is not None:
        expected["result"] = result
    return expected


def test_durable_run_transitions_match_python_store_and_recovery_oracle(
    tmp_path,
) -> None:
    from hermes_fleet.run_binding import RunBindingStore, recovery_action

    fixture: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema"] == "hermes-fleet.fleet-state-compat.v1"

    for index, scenario in enumerate(fixture["run_binding_scenarios"]):
        path = tmp_path / f"scenario-{index}.sqlite3"
        store = RunBindingStore(path)
        task_id = scenario["task_id"]
        for step in scenario["steps"]:
            action = step["action"]
            created = False
            if action == "reserve":
                binding, created = store.reserve_execution(task_id)
            elif action == "reopen":
                store = RunBindingStore(path)
                binding, created = store.reserve_execution(task_id)
            elif action == "bind":
                binding = store.bind_run(task_id, step["run_id"])
            elif action == "complete":
                binding = store.complete(task_id, step["run_id"], step["result"])
            elif action == "cancel":
                binding = store.mark_cancelled(task_id, step["run_id"])
            elif action == "indeterminate":
                binding = store.mark_indeterminate(task_id)
            else:
                raise AssertionError(f"unknown fixture action {action}")

            assert _expected_state(binding) == step["expected_state"]
            assert (
                recovery_action(binding, created=created) == step["expected_recovery"]
            )
