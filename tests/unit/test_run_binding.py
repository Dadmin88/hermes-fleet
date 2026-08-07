from __future__ import annotations


def test_run_binding_reservation_is_durable_and_duplicate_safe(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    path = tmp_path / "bindings.sqlite3"
    first = RunBindingStore(path)

    reserved, created = first.reserve_execution("task-1")
    reopened, created_again = RunBindingStore(path).reserve_execution("task-1")

    assert created is True
    assert created_again is False
    assert reserved.state == "creating"
    assert reserved.run_id is None
    assert reopened == reserved


def test_run_binding_records_a_known_hermes_run_for_resume(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")

    running = store.bind_run("task-1", "run-1")
    reopened = RunBindingStore(store.path).get("task-1")

    assert running.state == "running"
    assert running.run_id == "run-1"
    assert reopened == running


def test_run_binding_persists_terminal_text_for_keryx_replay(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")

    completed = store.complete("task-1", "run-1", "FLEET_OK")
    reopened = RunBindingStore(store.path).get("task-1")

    assert completed.state == "completed"
    assert completed.result_text == "FLEET_OK"
    assert reopened == completed


def test_run_binding_records_exact_cancelled_run_as_terminal(tmp_path) -> None:
    import pytest

    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")

    with pytest.raises(ValueError, match="run ID"):
        store.mark_cancelled("task-1", "run-other")

    cancelled = store.mark_cancelled("task-1", "run-1")
    reopened, created = RunBindingStore(store.path).reserve_execution("task-1")

    assert cancelled.state == "cancelled"
    assert cancelled.run_id == "run-1"
    assert reopened == cancelled
    assert created is False
    with pytest.raises(ValueError, match="state"):
        store.complete("task-1", "run-1", "late result")


def test_run_binding_migrates_existing_schema_for_cancelled_terminal_state(
    tmp_path,
) -> None:
    import sqlite3

    from hermes_fleet.run_binding import RunBindingStore

    path = tmp_path / "bindings.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE run_bindings (
                task_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                run_id TEXT,
                result_text TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (state IN ('creating', 'running', 'completed', 'indeterminate'))
            )
            """
        )
        connection.execute(
            "INSERT INTO run_bindings(task_id, state, run_id) VALUES (?, ?, ?)",
            ("task-1", "running", "run-1"),
        )

    store = RunBindingStore(path)
    cancelled = store.mark_cancelled("task-1", "run-1")

    assert cancelled.state == "cancelled"


def test_run_binding_rejects_lost_complete_transition(monkeypatch, tmp_path) -> None:
    import pytest

    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    store.mark_cancelled("task-1", "run-1")
    original_get = store._get
    calls = 0

    def stale_once(connection, task_id, *, required=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            from hermes_fleet.run_binding import RunBinding

            return RunBinding("task-1", "running", "run-1", None)
        return original_get(connection, task_id, required=required)

    monkeypatch.setattr(store, "_get", stale_once)

    with pytest.raises(ValueError, match="state"):
        store.complete("task-1", "run-1", "late result")

    assert RunBindingStore(store.path).get("task-1").state == "cancelled"


def test_run_binding_rejects_lost_cancel_transition(monkeypatch, tmp_path) -> None:
    import pytest

    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    store.complete("task-1", "run-1", "on-time result")
    original_get = store._get
    calls = 0

    def stale_once(connection, task_id, *, required=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            from hermes_fleet.run_binding import RunBinding

            return RunBinding("task-1", "running", "run-1", None)
        return original_get(connection, task_id, required=required)

    monkeypatch.setattr(store, "_get", stale_once)

    with pytest.raises(ValueError, match="state"):
        store.mark_cancelled("task-1", "run-1")

    binding = store.get("task-1")
    assert binding is not None
    assert binding.state == "completed"


def test_run_binding_rejects_lost_indeterminate_transition(
    monkeypatch,
    tmp_path,
) -> None:
    import pytest

    from hermes_fleet.run_binding import RunBinding, RunBindingStore

    path = tmp_path / "bindings.db"
    store = RunBindingStore(path)
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    store.mark_cancelled("task-1", "run-1")
    original_get = store._get
    calls = 0

    def stale_get(connection, task_id, *, required=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            return RunBinding(
                task_id="task-1",
                state="running",
                run_id="run-1",
                result_text=None,
            )
        return original_get(connection, task_id, required=required)

    monkeypatch.setattr(store, "_get", stale_get)

    with pytest.raises(ValueError, match="changed terminal state"):
        store.mark_indeterminate("task-1")

    binding = store.get("task-1")
    assert binding is not None
    assert binding.state == "cancelled"


def test_run_binding_marks_unknown_submission_indeterminate_without_retry(
    tmp_path,
) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")

    indeterminate = store.mark_indeterminate("task-1")
    reopened = RunBindingStore(store.path).reserve("task-1")

    assert indeterminate.state == "indeterminate"
    assert reopened == indeterminate


def test_run_binding_rejects_invalid_transitions(tmp_path) -> None:
    import pytest

    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")

    with pytest.raises(ValueError, match="reserved"):
        store.bind_run("task-1", "run-1")

    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    with pytest.raises(ValueError, match="state"):
        store.bind_run("task-1", "run-2")
    with pytest.raises(ValueError, match="run ID"):
        store.complete("task-1", "run-2", "wrong")
