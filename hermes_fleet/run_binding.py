"""Durable Keryx-task to Hermes-run bindings for duplicate-safe execution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_MAX_ID_CHARS = 256
_MAX_RESULT_CHARS = 65_536
_STATES = frozenset({"creating", "running", "completed", "indeterminate"})


@dataclass(frozen=True, slots=True)
class RunBinding:
    """One durable execution binding; it is not a Fleet task lifecycle."""

    task_id: str
    state: str
    run_id: str | None
    result_text: str | None


class RunBindingStore:
    """Persist only the data needed to prevent duplicate Hermes executions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.name:
            raise ValueError("binding store path must name a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_bindings (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    run_id TEXT,
                    result_text TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (
                        state IN ('creating', 'running', 'completed', 'indeterminate')
                    )
                )
                """
            )

    def reserve(self, task_id: str) -> RunBinding:
        """Reserve a task before run creation, returning any prior binding."""
        binding, _created = self.reserve_execution(task_id)
        return binding

    def reserve_execution(self, task_id: str) -> tuple[RunBinding, bool]:
        """Atomically reserve one execution and report whether this call created it."""
        task_id = _identifier(task_id, "task ID")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO run_bindings(task_id, state)
                VALUES (?, 'creating')
                """,
                (task_id,),
            )
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding, cursor.rowcount == 1

    def get(self, task_id: str) -> RunBinding | None:
        task_id = _identifier(task_id, "task ID")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id, state, run_id, result_text
                FROM run_bindings
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else _binding(row)

    def bind_run(self, task_id: str, run_id: str) -> RunBinding:
        """Record the one known Hermes run created for a reserved task."""
        task_id = _identifier(task_id, "task ID")
        run_id = _identifier(run_id, "run ID")
        with self._connect() as connection:
            existing = self._get(connection, task_id, required=False)
            if existing is None:
                raise ValueError("task must be reserved before binding a run")
            if existing.state == "running" and existing.run_id == run_id:
                return existing
            if existing.state != "creating":
                raise ValueError("task binding is not in the creating state")
            connection.execute(
                """
                UPDATE run_bindings
                SET state = 'running', run_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'creating'
                """,
                (run_id, task_id),
            )
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding

    def complete(self, task_id: str, run_id: str, result_text: str) -> RunBinding:
        """Persist terminal text before acknowledging completion to Keryx."""
        task_id = _identifier(task_id, "task ID")
        run_id = _identifier(run_id, "run ID")
        if type(result_text) is not str or len(result_text) > _MAX_RESULT_CHARS:
            raise ValueError("result text must be bounded text")
        with self._connect() as connection:
            existing = self._get(connection, task_id, required=False)
            if existing is None or existing.run_id != run_id:
                raise ValueError("Hermes run ID does not match the task binding")
            if existing.state == "completed" and existing.result_text == result_text:
                return existing
            if existing.state != "running":
                raise ValueError("task binding is not in the running state")
            connection.execute(
                """
                UPDATE run_bindings
                SET state = 'completed', result_text = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'running' AND run_id = ?
                """,
                (result_text, task_id, run_id),
            )
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding

    def mark_indeterminate(self, task_id: str) -> RunBinding:
        """Fail closed when run creation or resumed observation is uncertain."""
        task_id = _identifier(task_id, "task ID")
        with self._connect() as connection:
            existing = self._get(connection, task_id, required=False)
            if existing is None:
                raise ValueError("task must be reserved before becoming indeterminate")
            if existing.state == "indeterminate":
                return existing
            if existing.state not in {"creating", "running"}:
                raise ValueError(
                    "task binding cannot become indeterminate from this state"
                )
            connection.execute(
                """
                UPDATE run_bindings
                SET state = 'indeterminate', updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN ('creating', 'running')
                """,
                (task_id,),
            )
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _get(
        connection: sqlite3.Connection, task_id: str, *, required: bool = True
    ) -> RunBinding | None:
        row = connection.execute(
            """
            SELECT task_id, state, run_id, result_text
            FROM run_bindings
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            if required:
                raise RuntimeError("reserved task binding disappeared")
            return None
        return _binding(row)


def _identifier(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_ID_CHARS
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _binding(row: tuple[object, ...]) -> RunBinding:
    task_id, state, run_id, result_text = row
    if (
        type(task_id) is not str
        or type(state) is not str
        or state not in _STATES
        or (run_id is not None and type(run_id) is not str)
        or (result_text is not None and type(result_text) is not str)
    ):
        raise RuntimeError("binding store contains invalid data")
    return RunBinding(
        task_id=task_id,
        state=state,
        run_id=run_id,
        result_text=result_text,
    )
