"""SQLite checkpoints and immutable result objects, with a per-run process lock."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from filelock import FileLock, Timeout

from .contracts import RunEvent, RunRecord, ToolResult, new_id, now
from .errors import RunBusyError


class RunStore(Protocol):
    def create(self, run: RunRecord) -> None:
        """Create a run and atomically link it to its parent, if any."""
        ...

    def save(self, run: RunRecord, event: RunEvent | None = None) -> None: ...
    def load(self, run_id: str) -> RunRecord: ...
    def put_result(self, result: ToolResult) -> str: ...
    def get_result(self, reference: str) -> ToolResult: ...
    def events(self, run_id: str) -> list[RunEvent]: ...
    def exclusive(self, run_id: str) -> AbstractContextManager[None]: ...


class SQLiteRunStore:
    """Local durable storage. Keep the database and lock directory on local disk."""

    def __init__(self, path: str | Path = ".simagent/runs.sqlite3") -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_directory = self.path.parent / f"{self.path.name}.locks"
        self.lock_directory.mkdir(exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id), document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def exclusive(self, run_id: str) -> Iterator[None]:
        digest = hashlib.sha256(run_id.encode()).hexdigest()
        lock = FileLock(self.lock_directory / f"{digest}.lock", thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RunBusyError(f"Run {run_id} is already owned by another executor") from None
        try:
            yield
        finally:
            lock.release()

    def create(self, run: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if run.parent_run_id is not None:
                row = connection.execute(
                    "SELECT document FROM runs WHERE id=?", (run.parent_run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown parent run: {run.parent_run_id}")
                parent = RunRecord.model_validate_json(row[0])
                if parent.next_run_id is not None:
                    raise ValueError(f"Parent already has a revision: {parent.next_run_id}")
                parent.next_run_id, parent.updated_at = run.id, now()
                connection.execute(
                    "UPDATE runs SET document=? WHERE id=?", (parent.model_dump_json(), parent.id)
                )
            connection.execute("INSERT INTO runs VALUES (?, ?)", (run.id, run.model_dump_json()))
            connection.execute(
                "INSERT INTO events(run_id, document) VALUES (?, ?)",
                (
                    run.id,
                    RunEvent(kind="run.created").model_dump_json(),
                ),
            )

    def save(self, run: RunRecord, event: RunEvent | None = None) -> None:
        run.updated_at = now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET document=? WHERE id=?",
                (run.model_dump_json(), run.id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run: {run.id}")
            if event is not None:
                connection.execute(
                    "INSERT INTO events(run_id, document) VALUES (?, ?)",
                    (run.id, event.model_dump_json()),
                )

    def load(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return RunRecord.model_validate_json(row[0])

    def put_result(self, result: ToolResult) -> str:
        reference = new_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO results VALUES (?, ?)", (reference, result.model_dump_json())
            )
        return reference

    def get_result(self, reference: str) -> ToolResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM results WHERE id=?", (reference,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown result reference: {reference}")
        return ToolResult.model_validate_json(row[0])

    def events(self, run_id: str) -> list[RunEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [RunEvent.model_validate_json(row[0]) for row in rows]
