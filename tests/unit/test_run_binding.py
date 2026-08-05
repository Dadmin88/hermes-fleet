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
