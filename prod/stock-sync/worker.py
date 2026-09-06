"""Sequential stock-sync worker and local operations commands."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from stock_sync.config import Settings
from stock_sync.daily import run_daily
from stock_sync.db import Database
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.handlers import handle_import_meli_order, handle_reconcile_sku, handle_shopify_order
from stock_sync.meli import MeliClient
from stock_sync.shopify import ShopifyClient


logger = logging.getLogger('stock_sync.worker')


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def worker_lock(db: Database):
    """One job or full daily run at a time, including across processes.

    The sidecar transaction is released by SQLite on close or process death.
    It does not lock the event database or expire while an API call is running.
    """
    path = str(Path(db.path).resolve()) + '.worker-lock.db'
    connection = sqlite3.connect(path, timeout=0)
    try:
        try:
            connection.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise WorkerBusy('Another worker job or daily run is active; retry later.') from None
            raise
        yield
    finally:
        connection.close()


def retry_delay(attempt: int) -> int:
    return (60, 300, 1800, 7200)[min(max(attempt, 1), 4) - 1]


def safe_text(value: object) -> str:
    text = str(value)
    for key, secret in os.environ.items():
        if secret and any(part in key.upper() for part in ('TOKEN', 'SECRET', 'PASSWORD')):
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\bBearer\s+[^\s,;\'"}]+', 'Bearer [REDACTED]', text)
    text = re.sub(
        r'(?i)([\"\']?(?:[\w-]*(?:token|secret|password))[\"\']?\s*[:=]\s*)'
        r'(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s&,;}]+)',
        r'\1[REDACTED]', text,
    )
    return text


def _safe_output(value):
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {key: _safe_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_output(item) for item in value]
    return value


def log_state(db: Database, job_id: int | None, state: str) -> None:
    # Only controlled state names and numeric IDs enter transition logs.
    message = f'job {job_id}: {state}' if job_id is not None else f'daily: {state}'
    with db._connect() as connection:
        connection.execute(
            'INSERT INTO sync_logs (job_id, level, message, created_at) VALUES (?, ?, ?, ?)',
            (job_id, 'INFO', message, db._now()),
        )
    logger.info(message)


def _claim(db: Database, now: datetime, dry_run: bool):
    now_text = db._timestamp(now)
    if not dry_run:
        with db._connect() as connection:
            expired = connection.execute(
                "SELECT id FROM jobs WHERE status = 'processing' AND lease_until <= ?", (now_text,),
            ).fetchall()
        for expired_job in expired:
            db.retry_job(expired_job['id'], 'Processing lease expired', now, 0)
            log_state(db, expired_job['id'], 'retry_wait (expired lease)')
        return db.claim_next_job(now)

    # Inspecting must not spend a business attempt, even if the process dies.
    # Only the selected row may change; other expired leases belong to apply.
    with db._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        candidate = connection.execute(
            """SELECT * FROM jobs AS candidate
               WHERE ((status IN ('pending', 'retry_wait') AND available_at <= ?)
                      OR (status = 'processing' AND lease_until <= ?))
                 AND NOT EXISTS (
                     SELECT 1 FROM jobs AS locked
                     WHERE locked.resource_key = candidate.resource_key
                       AND locked.status = 'processing' AND locked.lease_until > ?)
               ORDER BY available_at, id LIMIT 1""", (now_text, now_text, now_text),
        ).fetchone()
        if candidate is None:
            return None
        connection.execute(
            "UPDATE jobs SET status = 'processing', lease_until = ?, updated_at = ? WHERE id = ?",
            (db._timestamp(now + timedelta(minutes=5)), now_text, candidate['id']),
        )
    return db.get_job(candidate['id'])


def _review_target(job):
    if job.job_type == 'import_meli_order' and job.payload.get('order_id') is not None:
        return f"order:{job.payload['order_id']}", None
    if job.job_type == 'shopify_order' and job.payload.get('id') is not None:
        return f"shopify-order:{job.payload['id']}", f"gid://shopify/Order/{job.payload['id']}"
    if job.job_type == 'reconcile_sku' and job.payload.get('shopify_order_id'):
        return f"sku:{job.payload.get('sku', '')}", job.payload['shopify_order_id']
    return f'product:worker-job-{job.id}', None


def _review_checkpoint(job_id: int) -> str:
    return f'worker_review:{job_id}'


def _clear_review_checkpoint(db: Database, job_id: int) -> None:
    with db._connect() as connection:
        connection.execute('DELETE FROM checkpoints WHERE checkpoint_key = ?', (_review_checkpoint(job_id),))


def _publish_review(db, shopify, job, notice, now):
    key, order_id = _review_target(job)
    try:
        if order_id:
            shopify.mark_order_review(order_id, key, notice)
        else:
            draft_id = shopify.create_or_update_review(key, notice)
            db.link_review(key, draft_id)
    except (RetryableSyncError, ReviewRequiredError) as error:
        # Permanent Shopify permission failures also cannot lose the notice.
        db.retry_job(job.id, safe_text(f'{notice}; Review publication failed: {error}'), now, 7200)
        log_state(db, job.id, 'retry_wait (review publication)')
        return {'status': 'retry_wait', 'error': safe_text(error)}
    db.needs_review(job.id, key, notice)
    log_state(db, job.id, 'needs_review')
    _clear_review_checkpoint(db, job.id)
    return {'status': 'needs_review', 'review_key': key}


def _require_review(db, shopify, job, error, now):
    notice = safe_text(error)
    # Persist the notice before publishing: retries resume publication only.
    db.set_checkpoint(_review_checkpoint(job.id), notice)
    return _publish_review(db, shopify, job, notice, now)


def _route(job, db, shopify, meli, dry_run):
    if job.job_type == 'import_meli_order':
        return handle_import_meli_order(job, db, shopify, meli, dry_run=dry_run)
    if job.job_type == 'shopify_order':
        return handle_shopify_order(job, db, shopify, dry_run=dry_run)
    if job.job_type == 'reconcile_sku':
        return handle_reconcile_sku(job, db, shopify, meli, dry_run=dry_run)
    raise ReviewRequiredError(f'product:worker-job-{job.id}', 'Unknown job type', {})


def process_one(db, shopify, meli, *, now=None, dry_run=False):
    with worker_lock(db):
        job = _claim(db, now or datetime.now(timezone.utc), dry_run)
        if job is None:
            return None
        log_state(db, job.id, 'processing (dry run)' if dry_run else 'processing')
        failure_time = lambda: now or datetime.now(timezone.utc)
        try:
            notice = db.get_checkpoint(_review_checkpoint(job.id))
            if notice is not None:
                result = ({'status': 'needs_review', 'error': notice} if dry_run else
                          _publish_review(db, shopify, job, notice, failure_time()))
            else:
                result = _route(job, db, shopify, meli, dry_run)
                if not dry_run:
                    persisted = db.get_job(job.id)
                    if persisted.status == 'needs_review':
                        log_state(db, job.id, 'needs_review')
                    elif result.get('status') == 'needs_review':
                        result = _require_review(db, shopify, job,
                                                 '; '.join(result.get('problems', ['Handler requires review'])), failure_time())
                    elif persisted.status == 'processing':
                        db.complete_job(job.id)
                        log_state(db, job.id, 'completed')
        except Exception as error:
            if isinstance(error, (RetryableSyncError, ReviewRequiredError)):
                message = safe_text(error)
            else:
                message = f'Unexpected handler failure ({type(error).__name__})'
            if dry_run:
                result = {'status': 'needs_review' if isinstance(error, ReviewRequiredError) else 'retry_wait', 'error': message}
            elif isinstance(error, ReviewRequiredError) or job.attempts >= 5:
                result = _require_review(db, shopify, job, message, failure_time())
            else:
                db.retry_job(job.id, message, failure_time(), retry_delay(job.attempts))
                log_state(db, job.id, 'retry_wait')
                result = {'status': 'retry_wait', 'error': message}
        finally:
            if dry_run:
                with db._connect() as connection:
                    connection.execute(
                        "UPDATE jobs SET status = 'pending', lease_until = NULL, updated_at = ? WHERE id = ?",
                        (db._now(), job.id),
                    )
                log_state(db, job.id, 'pending (dry run inspected)')
        return {'job_id': job.id, 'dry_run': dry_run, **result}


def process_daily(db, shopify, meli, *, now=None, dry_run=False):
    with worker_lock(db):
        log_state(db, None, 'processing (dry run)' if dry_run else 'processing')
        try:
            result = run_daily(db, shopify, meli, now or datetime.now(timezone.utc), dry_run=dry_run)
        except Exception:
            log_state(db, None, 'failed')
            raise
        log_state(db, None, result.status)
        return result


def run_loop(db, shopify, meli):
    stopping = Event()
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        for signum in previous:
            signal.signal(signum, lambda *_: stopping.set())
        while not stopping.is_set():
            try:
                result = process_one(db, shopify, meli)
            except WorkerBusy:
                result = None
            if result is None:
                stopping.wait(1)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def retry_review(db: Database, job_id: int) -> bool:
    with worker_lock(db), db._connect() as connection:
        cursor = connection.execute(
            """UPDATE jobs SET status = 'pending', attempts = 0, last_error = NULL,
               lease_until = NULL, available_at = ?, updated_at = ?
               WHERE id = ? AND status = 'needs_review'""", (db._now(), db._now(), job_id),
        )
        if not cursor.rowcount:
            return False
        connection.execute('DELETE FROM checkpoints WHERE checkpoint_key = ?', (_review_checkpoint(job_id),))
    log_state(db, job_id, 'pending (manual retry)')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('migrate', 'run', 'list-review'):
        commands.add_parser(command)
    for command in ('once', 'daily'):
        commands.add_parser(command).add_argument('--dry-run', action='store_true')
    commands.add_parser('retry').add_argument('job_id', type=int)
    args = parser.parse_args(argv)
    try:
        db = Database(os.environ.get('STOCK_SYNC_DATABASE', 'prod/stock-sync/data/stock_sync.db'))
        if args.command == 'migrate':
            print('Database migrated.')
        elif args.command == 'list-review':
            with db._connect() as connection:
                reviews = connection.execute(
                    "SELECT id, job_type, source_key, attempts, last_error FROM jobs WHERE status = 'needs_review' ORDER BY id",
                ).fetchall()
            for review in reviews:
                print(json.dumps(_safe_output(dict(review))))
        elif args.command == 'retry':
            if not retry_review(db, args.job_id):
                print('Job does not exist or is not awaiting review.')
                return 1
            print(f'Job {args.job_id} returned to pending.')
        else:
            settings = Settings.from_env()
            shopify, meli = ShopifyClient(settings), MeliClient(settings)
            if args.command == 'run':
                run_loop(db, shopify, meli)
            elif args.command == 'once':
                result = process_one(db, shopify, meli, dry_run=args.dry_run)
                print(json.dumps(_safe_output(result if result is not None else {'status': 'idle'})))
            else:
                result = process_daily(db, shopify, meli, dry_run=args.dry_run)
                print(json.dumps(_safe_output(asdict(result))))
        return 0
    except WorkerBusy as error:
        print(str(error))
        return 2
    except (RetryableSyncError, ReviewRequiredError, ValueError, OSError, sqlite3.Error) as error:
        print(safe_text(error))
        return 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    raise SystemExit(main())
