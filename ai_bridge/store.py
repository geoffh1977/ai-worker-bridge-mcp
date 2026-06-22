from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .task_state import TaskRecord, TaskState, utc_now


class TaskStore:
    def __init__(self, sqlite_path: str):
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    working_directory TEXT,
                    idempotency_key TEXT UNIQUE,
                    dispatch_attempt_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    timeout_seconds REAL NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "working_directory" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN working_directory TEXT")
            if "dispatch_attempt_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN dispatch_attempt_id TEXT")
            if "attempt_count" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_worker ON tasks(worker_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_worker_state ON tasks(worker_id, state)")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            worker_id=row["worker_id"],
            prompt=row["prompt"],
            working_directory=row["working_directory"],
            idempotency_key=row["idempotency_key"],
            dispatch_attempt_id=row["dispatch_attempt_id"],
            attempt_count=row["attempt_count"],
            state=TaskState(row["state"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            timeout_seconds=row["timeout_seconds"],
        )

    def upsert(self, task: TaskRecord) -> TaskRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, worker_id, prompt, working_directory, idempotency_key,
                                  dispatch_attempt_id, attempt_count, state, result_json, error,
                                  created_at, updated_at, started_at, completed_at, timeout_seconds)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    prompt=excluded.prompt,
                    working_directory=excluded.working_directory,
                    idempotency_key=excluded.idempotency_key,
                    dispatch_attempt_id=excluded.dispatch_attempt_id,
                    attempt_count=excluded.attempt_count,
                    state=excluded.state,
                    result_json=excluded.result_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    timeout_seconds=excluded.timeout_seconds
                """,
                self._params(task),
            )
        return task

    def create_or_get(self, task: TaskRecord) -> tuple[TaskRecord, bool]:
        """Atomically insert a task or return the existing idempotent task."""
        if not task.idempotency_key:
            self.upsert(task)
            return task, True
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (task.idempotency_key,)).fetchone()
                if existing:
                    conn.execute("COMMIT")
                    return self._row_to_record(existing), False
                conn.execute(
                    """
                    INSERT INTO tasks(task_id, worker_id, prompt, working_directory, idempotency_key,
                                      dispatch_attempt_id, attempt_count, state, result_json, error,
                                      created_at, updated_at, started_at, completed_at, timeout_seconds)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._params(task),
                )
                conn.execute("COMMIT")
                return task, True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _params(task: TaskRecord) -> tuple[object, ...]:
        return (
            task.task_id,
            task.worker_id,
            task.prompt,
            task.working_directory,
            task.idempotency_key,
            task.dispatch_attempt_id,
            task.attempt_count,
            task.state.value,
            json.dumps(task.result) if task.result is not None else None,
            task.error,
            task.created_at,
            task.updated_at,
            task.started_at,
            task.completed_at,
            task.timeout_seconds,
        )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._row_to_record(row) if row else None

    def get_by_idempotency_key(self, key: str) -> TaskRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
            return self._row_to_record(row) if row else None

    def list_by_states(self, states: Iterable[TaskState]) -> list[TaskRecord]:
        values = [s.value for s in states]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        with self._lock, self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM tasks WHERE state IN ({placeholders})", values).fetchall()
            return [self._row_to_record(r) for r in rows]

    def count_by_states(self, states: Iterable[TaskState], *, worker_id: str | None = None) -> int:
        values = [s.value for s in states]
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        params: list[object] = list(values)
        sql = f"SELECT COUNT(*) FROM tasks WHERE state IN ({placeholders})"
        if worker_id is not None:
            sql += " AND worker_id=?"
            params.append(worker_id)
        with self._lock, self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def mark_recovering(self) -> list[TaskRecord]:
        recoverable = self.list_by_states([TaskState.PENDING, TaskState.RUNNING, TaskState.RETRYING, TaskState.RECOVERING])
        updated: list[TaskRecord] = []
        for task in recoverable:
            if task.state == TaskState.PENDING:
                updated.append(task)
                continue
            task.state = TaskState.RECOVERING
            task.updated_at = utc_now()
            task.dispatch_attempt_id = None
            updated.append(self.upsert(task))
        return updated

    def ping(self) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS readiness_probe (id INTEGER PRIMARY KEY, updated_at TEXT)")
            conn.execute(
                "INSERT INTO readiness_probe(id, updated_at) VALUES(1, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (utc_now(),),
            )
            conn.execute("SELECT 1")
        return True
