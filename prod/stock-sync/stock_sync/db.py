from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Job


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript((Path(__file__).parent.parent / "schema.sql").read_text())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    @classmethod
    def _now(cls) -> str:
        return cls._timestamp(datetime.now(timezone.utc))

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            job_type=row["job_type"],
            source_key=row["source_key"],
            resource_key=row["resource_key"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            attempts=row["attempts"],
            available_at=row["available_at"],
            last_error=row["last_error"],
        )

    def enqueue_job(
        self, job_type: str, source_key: str, payload: dict[str, Any], resource_key: str
    ) -> int:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_type, source_key, resource_key, payload, status, attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(job_type, source_key) DO NOTHING
                """,
                (job_type, source_key, resource_key, json.dumps(payload, sort_keys=True), now, now, now),
            )
            row = connection.execute(
                "SELECT id FROM jobs WHERE job_type = ? AND source_key = ?", (job_type, source_key)
            ).fetchone()
        return int(row["id"])

    def claim_next_job(self, now: datetime) -> Job | None:
        now_text = self._timestamp(now)
        lease_until = self._timestamp(now + timedelta(minutes=5))
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'retry_wait', lease_until = NULL, available_at = ?, updated_at = ?
                    WHERE status = 'processing' AND lease_until <= ?
                    """,
                    (now_text, now_text, now_text),
                )
                row = connection.execute(
                    """
                    SELECT * FROM jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry_wait')
                      AND candidate.available_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs AS locked
                          WHERE locked.resource_key = candidate.resource_key
                            AND locked.status = 'processing'
                            AND locked.lease_until > ?
                      )
                    ORDER BY candidate.available_at, candidate.id
                    LIMIT 1
                    """,
                    (now_text, now_text),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'processing', attempts = attempts + 1, lease_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (lease_until, now_text, row["id"]),
                )
                claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
                connection.commit()
                return self._job(claimed)
            except Exception:
                connection.rollback()
                raise

    def complete_job(self, job_id: int) -> None:
        self._set_job_status(job_id, "completed")

    def retry_job(self, job_id: int, error: str, now: datetime, delay_seconds: int) -> None:
        available_at = self._timestamp(now + timedelta(seconds=delay_seconds))
        updated_at = self._timestamp(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'retry_wait', available_at = ?, lease_until = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (available_at, error, updated_at, job_id),
            )

    def needs_review(self, job_id: int, review_key: str, error: str) -> None:
        del review_key
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'needs_review', lease_until = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, self._now(), job_id),
            )

    def _set_job_status(self, job_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = ?, lease_until = NULL, updated_at = ? WHERE id = ?",
                (status, self._now(), job_id),
            )

    def get_job(self, job_id: int) -> Job:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        return self._job(row)

    def count(self, table: str) -> int:
        allowed_tables = {"events", "jobs", "order_links", "review_links", "sync_logs", "checkpoints"}
        if table not in allowed_tables:
            raise ValueError(f"Unknown table: {table}")
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def link_order(self, meli_order_id: str, shopify_order_id: str) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_links (meli_order_id, shopify_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(meli_order_id) DO UPDATE SET
                    shopify_order_id = excluded.shopify_order_id,
                    updated_at = excluded.updated_at
                """,
                (meli_order_id, shopify_order_id, now, now),
            )

    def get_order_link(self, meli_order_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT shopify_order_id FROM order_links WHERE meli_order_id = ?", (meli_order_id,)
            ).fetchone()
        return None if row is None else str(row["shopify_order_id"])

    def begin_order_create(self, meli_order_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO order_creates (meli_order_id, started_at) VALUES (?, ?)",
                (meli_order_id, self._now()),
            )
        return bool(result.rowcount)

    def has_order_create(self, meli_order_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM order_creates WHERE meli_order_id = ?", (meli_order_id,),
            ).fetchone() is not None

    def clear_rejected_order_create(self, meli_order_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM order_creates WHERE meli_order_id = ?", (meli_order_id,))

    def complete_import_reviews(self, meli_order_id: str) -> None:
        """A later notice can repair the import reviewed by an earlier job."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE jobs SET status = 'completed', last_error = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE job_type = 'import_meli_order' AND resource_key = ?
                     AND (status = 'needs_review' OR EXISTS (
                       SELECT 1 FROM checkpoints WHERE checkpoint_key = 'worker_review:' || jobs.id))""",
                (self._now(), f"order:{meli_order_id}"),
            )
            connection.execute(
                """DELETE FROM checkpoints WHERE checkpoint_key IN (
                     SELECT 'worker_review:' || id FROM jobs
                     WHERE job_type = 'import_meli_order' AND resource_key = ?)""",
                (f"order:{meli_order_id}",),
            )

    def record_order_review(self, order_id: str, review_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO order_review_links VALUES (?, ?)", (order_id, review_key),
            )

    def has_order_review(self, order_id: str, review_key: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM order_review_links WHERE shopify_order_id = ? AND review_key = ?",
                (order_id, review_key),
            ).fetchone() is not None

    def clear_order_review(self, order_id: str, review_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM order_review_links WHERE shopify_order_id = ? AND review_key = ?",
                (order_id, review_key),
            )

    def link_review(self, review_key: str, shopify_draft_order_id: str) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_links (review_key, shopify_draft_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(review_key) DO UPDATE SET
                    shopify_draft_order_id = excluded.shopify_draft_order_id,
                    updated_at = excluded.updated_at
                """,
                (review_key, shopify_draft_order_id, now, now),
            )

    def get_review_link(self, review_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT shopify_draft_order_id FROM review_links WHERE review_key = ?", (review_key,)
            ).fetchone()
        return None if row is None else str(row["shopify_draft_order_id"])

    def get_checkpoint(self, checkpoint_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT checkpoint_value FROM checkpoints WHERE checkpoint_key = ?", (checkpoint_key,)
            ).fetchone()
        return None if row is None else str(row["checkpoint_value"])

    def set_checkpoint(self, checkpoint_key: str, checkpoint_value: str) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (checkpoint_key, checkpoint_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    checkpoint_value = excluded.checkpoint_value,
                    updated_at = excluded.updated_at
                """,
                (checkpoint_key, checkpoint_value, now),
            )
