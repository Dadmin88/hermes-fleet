"""Durable Keryx-task to Hermes-run bindings for duplicate-safe execution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_MAX_ID_CHARS = 256
_MAX_RESULT_CHARS = 65_536
_STATES = frozenset(
    {"creating", "running", "completed", "cancelled", "indeterminate", "resolved"}
)
_SCHEMA_SQL = """
CREATE TABLE run_bindings (
    task_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    run_id TEXT,
    result_text TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        state IN (
            'creating', 'running', 'completed', 'cancelled', 'indeterminate', 'resolved'
        )
    )
)
"""


@dataclass(frozen=True, slots=True)
class RunBinding:
    """One durable execution binding; it is not a Fleet task lifecycle."""

    task_id: str
    state: str
    run_id: str | None
    result_text: str | None


RecoveryAction = Literal[
    "start_new",
    "resume_known_run",
    "replay_completed",
    "fail_cancelled",
    "fail_closed_indeterminate",
]


def recovery_action(binding: RunBinding, *, created: bool) -> RecoveryAction:
    """Classify duplicate-safe worker recovery from durable binding truth."""
    if binding.state == "completed":
        if binding.run_id is not None and binding.result_text is not None:
            return "replay_completed"
        return "fail_closed_indeterminate"
    if binding.state == "creating":
        return "start_new" if created else "fail_closed_indeterminate"
    if binding.state == "running":
        if binding.run_id is not None:
            return "resume_known_run"
        return "fail_closed_indeterminate"
    if binding.state == "cancelled":
        return "fail_cancelled"
    return "fail_closed_indeterminate"


class RunBindingStore:
    """Persist only the data needed to prevent duplicate Hermes executions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.name:
            raise ValueError("binding store path must name a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'run_bindings'"
            ).fetchone()
            if row is None:
                connection.execute(_SCHEMA_SQL)
            elif "'resolved'" not in str(row[0]):
                connection.execute(
                    "ALTER TABLE run_bindings RENAME TO run_bindings_legacy"
                )
                connection.execute(_SCHEMA_SQL)
                connection.execute(
                    """
                    INSERT INTO run_bindings(
                        task_id, state, run_id, result_text, updated_at
                    )
                    SELECT task_id, state, run_id, result_text, updated_at
                    FROM run_bindings_legacy
                    """
                )
                connection.execute("DROP TABLE run_bindings_legacy")

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

    def reserve_execution_if_available(
        self, task_id: str, *, max_unresolved: int
    ) -> tuple[RunBinding | None, bool]:
        """Reserve a new task only when a Fleet-owned execution slot is free."""
        task_id = _identifier(task_id, "task ID")
        if isinstance(max_unresolved, bool) or not isinstance(max_unresolved, int):
            raise ValueError("max unresolved bindings must be an integer")
        if max_unresolved < 1 or max_unresolved > 1_024:
            raise ValueError("max unresolved bindings is outside the supported range")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get(connection, task_id, required=False)
            if existing is not None:
                return existing, False
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM run_bindings
                WHERE state IN ('creating', 'running', 'indeterminate')
                """
            ).fetchone()
            assert row is not None
            if int(row[0]) >= max_unresolved:
                return None, False
            connection.execute(
                """
                INSERT INTO run_bindings(task_id, state)
                VALUES (?, 'creating')
                """,
                (task_id,),
            )
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding, True

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

    def unresolved_count(self) -> int:
        """Count bindings whose Hermes execution may still consume Fleet capacity."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM run_bindings
                WHERE state IN ('creating', 'running', 'indeterminate')
                """
            ).fetchone()
        assert row is not None
        return int(row[0])

    def indeterminate_bindings(self) -> tuple[tuple[RunBinding, int], ...]:
        """Return uncertain bindings with their durable update timestamps."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, state, run_id, result_text, updated_at
                FROM run_bindings
                WHERE state = 'indeterminate'
                ORDER BY task_id
                """
            ).fetchall()
        values = []
        for row in rows:
            binding = _binding(row[:4])
            try:
                updated = datetime.strptime(str(row[4]), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "binding store contains invalid timestamp"
                ) from error
            values.append((binding, int(updated.timestamp() * 1_000)))
        return tuple(values)

    def resolve_indeterminate(self, task_id: str) -> RunBinding:
        """Retain proven terminal uncertainty without reserving execution capacity."""
        task_id = _identifier(task_id, "task ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get(connection, task_id, required=False)
            if existing is None:
                raise ValueError("task binding does not exist")
            if existing.state == "resolved":
                return existing
            if existing.state != "indeterminate":
                raise ValueError("only indeterminate bindings can be resolved")
            cursor = connection.execute(
                """
                UPDATE run_bindings
                SET state = 'resolved', updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'indeterminate'
                """,
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("task binding changed terminal state")
            resolved = self._get(connection, task_id)
            assert resolved is not None
            return resolved

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
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get(connection, task_id, required=False)
            if existing is None or existing.run_id != run_id:
                raise ValueError("Hermes run ID does not match the task binding")
            if existing.state == "completed" and existing.result_text == result_text:
                return existing
            if existing.state != "running":
                raise ValueError("task binding is not in the running state")
            cursor = connection.execute(
                """
                UPDATE run_bindings
                SET state = 'completed', result_text = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'running' AND run_id = ?
                """,
                (result_text, task_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("task binding changed terminal state")
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding

    def mark_cancelled(self, task_id: str, run_id: str) -> RunBinding:
        """Persist confirmed deadline cancellation for one exact bound run."""
        task_id = _identifier(task_id, "task ID")
        run_id = _identifier(run_id, "run ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get(connection, task_id, required=False)
            if existing is None or existing.run_id != run_id:
                raise ValueError("Hermes run ID does not match the task binding")
            if existing.state == "cancelled":
                return existing
            if existing.state != "running":
                raise ValueError("task binding is not in the running state")
            cursor = connection.execute(
                """
                UPDATE run_bindings
                SET state = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'running' AND run_id = ?
                """,
                (task_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("task binding changed terminal state")
            binding = self._get(connection, task_id)
            assert binding is not None
            return binding

    def mark_indeterminate(self, task_id: str) -> RunBinding:
        """Fail closed when run creation or resumed observation is uncertain."""
        task_id = _identifier(task_id, "task ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get(connection, task_id, required=False)
            if existing is None:
                raise ValueError("task must be reserved before becoming indeterminate")
            if existing.state == "indeterminate":
                return existing
            if existing.state not in {"creating", "running"}:
                raise ValueError(
                    "task binding cannot become indeterminate from this state"
                )
            cursor = connection.execute(
                """
                UPDATE run_bindings
                SET state = 'indeterminate', updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN ('creating', 'running')
                """,
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("task binding changed terminal state")
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
