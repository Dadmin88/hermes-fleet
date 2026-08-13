from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

import grpc


@dataclass
class _Status:
    value: str


class _Handle:
    def __init__(self, status: str) -> None:
        self._status = status

    async def refresh(self):
        return type("Task", (), {"status": _Status(self._status)})()


class _Node:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    def task_handle(self, task_id: str) -> _Handle:
        return _Handle(self._statuses[task_id])


class _UnavailableNode:
    def task_handle(self, _task_id: str):
        class Handle:
            async def refresh(self):
                raise grpc.RpcError("daemon unavailable")

        return Handle()


class _Hermes:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    def status(self, run_id: str) -> str:
        return self._statuses[run_id]


def _age(store, task_id: str, timestamp: str) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE run_bindings SET updated_at = ? WHERE task_id = ?",
            (timestamp, task_id),
        )


def test_known_running_run_remains_capacity_consuming(tmp_path) -> None:
    from hermes_fleet.node_service import _reconcile_indeterminate_bindings
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    store.mark_indeterminate("task-1")

    asyncio.run(
        _reconcile_indeterminate_bindings(
            store,
            _Node({"task-1": "failed"}),
            _Hermes({"run-1": "running"}),
            now_ms=2_000_000,
        )
    )

    assert store.get("task-1").state == "indeterminate"
    assert store.unresolved_count() == 1


def test_keryx_refresh_failure_retains_binding_fail_closed(tmp_path) -> None:
    from hermes_fleet.node_service import _reconcile_indeterminate_bindings
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.mark_indeterminate("task-1")

    asyncio.run(
        _reconcile_indeterminate_bindings(
            store,
            _UnavailableNode(),
            _Hermes({}),
            now_ms=2_000_000,
        )
    )

    assert store.get("task-1").state == "indeterminate"
    assert store.unresolved_count() == 1


def test_known_missing_run_and_terminal_task_resolve_idempotently(tmp_path) -> None:
    from hermes_fleet.node_service import _reconcile_indeterminate_bindings
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.bind_run("task-1", "run-1")
    store.mark_indeterminate("task-1")

    for _ in range(2):
        asyncio.run(
            _reconcile_indeterminate_bindings(
                store,
                _Node({"task-1": "failed"}),
                _Hermes({"run-1": "missing"}),
                now_ms=2_000_000,
            )
        )

    assert store.get("task-1").state == "resolved"
    assert store.unresolved_count() == 0


def test_runless_recent_terminal_uncertainty_remains_fail_closed(tmp_path) -> None:
    from hermes_fleet.node_service import _reconcile_indeterminate_bindings
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.mark_indeterminate("task-1")
    _age(store, "task-1", "1970-01-01 00:29:00")

    asyncio.run(
        _reconcile_indeterminate_bindings(
            store,
            _Node({"task-1": "failed"}),
            _Hermes({}),
            now_ms=2_000_000,
        )
    )

    assert store.get("task-1").state == "indeterminate"


def test_runless_expired_terminal_uncertainty_resolves(tmp_path) -> None:
    from hermes_fleet.node_service import _reconcile_indeterminate_bindings
    from hermes_fleet.run_binding import RunBindingStore

    store = RunBindingStore(tmp_path / "bindings.sqlite3")
    store.reserve("task-1")
    store.mark_indeterminate("task-1")
    _age(store, "task-1", "1970-01-01 00:00:00")

    asyncio.run(
        _reconcile_indeterminate_bindings(
            store,
            _Node({"task-1": "failed"}),
            _Hermes({}),
            now_ms=2_000_000,
        )
    )

    assert store.get("task-1").state == "resolved"
    assert store.unresolved_count() == 0
