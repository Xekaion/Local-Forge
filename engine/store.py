from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
PUBLIC_JOB_FIELDS = (
    "id",
    "status",
    "progress",
    "stage",
    "model_url",
    "manifest_url",
    "error",
    "input_sha256",
    "output_sha256",
    "manifest_sha256",
    "output_bytes",
    "created_at",
    "updated_at",
)
MUTABLE_FIELDS = {
    "status",
    "progress",
    "stage",
    "model_url",
    "manifest_url",
    "error",
    "output_path",
    "manifest_path",
    "output_sha256",
    "output_bytes",
    "cancel_requested",
}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class RuntimeLock:
    """An OS-released lock that prevents multiple GPU workers per runtime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None
        self._mutex = threading.Lock()

    def acquire(self) -> bool:
        with self._mutex:
            if self._handle is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                handle.close()
                return False
            self._handle = handle
            return True

    def release(self) -> None:
        with self._mutex:
            handle = self._handle
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self._handle = None


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    model_url TEXT,
                    manifest_url TEXT,
                    error TEXT,
                    input_sha256 TEXT NOT NULL,
                    output_sha256 TEXT,
                    manifest_sha256 TEXT,
                    output_bytes INTEGER,
                    input_path TEXT NOT NULL,
                    output_path TEXT,
                    manifest_path TEXT,
                    input_bytes INTEGER NOT NULL,
                    image_width INTEGER NOT NULL,
                    image_height INTEGER NOT NULL,
                    source_format TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    remesh INTEGER NOT NULL,
                    texture INTEGER NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (status IN (
                        'queued', 'running', 'succeeded', 'failed', 'cancelled'
                    ))
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "manifest_sha256" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN manifest_sha256 TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created "
                "ON jobs(status, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)"
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        return {
            field: job[field]
            for field in PUBLIC_JOB_FIELDS
            if job.get(field) is not None
        }

    def create(self, job: dict[str, Any]) -> dict[str, Any]:
        columns = tuple(job.keys())
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})"
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(sql, tuple(job[column] for column in columns))
            connection.commit()
        created = self.get(job["id"])
        assert created is not None
        return created

    def get(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row(row)

    def get_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._row(row)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        unknown = set(changes) - MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported job fields: {sorted(unknown)}")
        if "status" in changes and changes["status"] not in JOB_STATUSES:
            raise ValueError(f"Unsupported status: {changes['status']}")
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{field} = ?" for field in changes)
        values = [changes[field] for field in changes]
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values, job_id),
            )
            connection.commit()
        return self.get(job_id)

    def claim(self, job_id: str) -> bool:
        now = utc_now()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'running', progress = 8, stage = '입력 이미지 분석',
                    updated_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                """,
                (now, job_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def complete_success(
        self,
        job_id: str,
        *,
        model_url: str,
        manifest_url: str,
        output_path: str,
        manifest_path: str,
        output_sha256: str,
        manifest_sha256: str,
        output_bytes: int,
    ) -> bool:
        now = utc_now()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', progress = 100, stage = '완료',
                    model_url = ?, manifest_url = ?, output_path = ?,
                    manifest_path = ?, output_sha256 = ?,
                    manifest_sha256 = ?, output_bytes = ?, error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND cancel_requested = 0
                """,
                (
                    model_url,
                    manifest_url,
                    output_path,
                    manifest_path,
                    output_sha256,
                    manifest_sha256,
                    output_bytes,
                    now,
                    job_id,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def complete_failure_or_cancellation(
        self,
        job_id: str,
        *,
        error: str | None,
    ) -> str | None:
        now = utc_now()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] != "running":
                connection.rollback()
                return str(row["status"])
            if row["cancel_requested"]:
                status = "cancelled"
                stage = "취소됨"
                final_error = None
            else:
                status = "failed"
                stage = "실패"
                final_error = error
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, stage, final_error, now, job_id),
            )
            connection.commit()
        return status

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            status = row["status"]
            if status == "queued":
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', stage = '취소됨',
                        cancel_requested = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
            elif status == "running":
                connection.execute(
                    """
                    UPDATE jobs
                    SET stage = '취소 요청됨', cancel_requested = 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
            connection.commit()
        return self.get(job_id)

    def should_cancel(self, job_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT cancel_requested, status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))

    def count_active(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM jobs "
                "WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row["total"])

    def queued(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cancelled = connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', stage = '취소됨', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 1
                """,
                (now,),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', stage = 'interrupted',
                    error = '엔진 재시작으로 실행이 중단되었습니다.',
                    updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                """,
                (now,),
            ).rowcount
            connection.commit()
        return int(cancelled + failed)

    def expired_terminal(self, cutoff: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('succeeded', 'failed', 'cancelled')
                  AND updated_at < ?
                ORDER BY updated_at
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, job_id: str) -> None:
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM jobs WHERE id = ? "
                "AND status IN ('succeeded', 'failed', 'cancelled')",
                (job_id,),
            )
            connection.commit()

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in JOB_STATUSES}
        with closing(self._connect()) as connection:
            rows: Iterable[sqlite3.Row] = connection.execute(
                "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["total"])
        return counts

    def dump_json(self, job_id: str) -> str:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return json.dumps(job, ensure_ascii=False, sort_keys=True)
