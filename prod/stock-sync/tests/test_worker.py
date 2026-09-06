from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import worker
from stock_sync.db import Database
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.models import MeliListing, MeliOrder, MeliOrderLine, ShopifyVariant


class Shopify:
    def __init__(self, db):
        self.db = db
        self.quantity = 8
        self.orders = {}
        self.drafts = {}
        self.order_reviews = {}
        self.review_error = None
        self.quantity_error = None
        self.before_review = None

    def find_variants_by_skus(self, skus):
        return {sku: [ShopifyVariant('gid://shopify/ProductVariant/1', sku, self.quantity, True)] for sku in skus}

    def find_imported_order(self, order_id):
        return self.orders.get(order_id)

    def create_imported_order(self, order, variants):
        self.orders[order.order_id] = 'gid://shopify/Order/77'
        self.quantity -= sum(line.quantity for line in order.lines)
        return self.orders[order.order_id]

    def get_available_quantity(self, sku):
        if self.quantity_error:
            raise self.quantity_error
        return self.quantity

    def _review(self):
        if self.before_review:
            self.before_review()
        if self.review_error:
            raise self.review_error

    def create_or_update_review(self, key, note, *, draft_id=None):
        self._review()
        self.drafts[key] = note
        return 'gid://shopify/DraftOrder/99'

    def mark_order_review(self, order_id, key, note):
        self._review()
        self.order_reviews[(order_id, key)] = note

    def list_all_variants(self):
        return [ShopifyVariant('gid://shopify/ProductVariant/1', 'ABC', self.quantity, True)]

    def resolve_review(self, key, *, draft_id=None, shopify_order_id=None):
        self.drafts.pop(key, None)

    def get_order_skus(self, order_id):
        return ["ABC"]

    def resolve_order_review(self, order_id, key):
        self.order_reviews.pop((order_id, key), None)


class Meli:
    def __init__(self):
        self.error = None
        self.order = MeliOrder('2001', '100', 'paid', '2026-09-06T00:00:00+00:00', [
            MeliOrderLine('MLC1', None, 'Widget', 2, '1000', 'CLP', 'ABC')])
        self.listings = [MeliListing('MLC1', None, 'ABC', 10)]
        self.reads = 0
        self.writes = []
        self.paid_orders = []
        self.on_search = None

    def get_order(self, order_id):
        self.reads += 1
        if self.error:
            raise self.error
        return self.order

    def list_all_listings(self):
        return list(self.listings)

    def set_available_quantity(self, listing, quantity):
        self.writes.append((listing.sku, quantity))
        self.listings = [replace(listing, available_quantity=quantity)]

    def search_paid_orders(self, since):
        if self.on_search:
            self.on_search()
        return self.paid_orders


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db=db, shopify=Shopify(db), meli=Meli(), now=datetime.now(timezone.utc) + timedelta(seconds=1))


def enqueue(ctx, kind='import_meli_order', attempts=0, payload=None):
    defaults = {
        'import_meli_order': ('meli-order:2001', 'order:2001', {'order_id': '2001'}),
        'shopify_order': ('shopify-order:88', 'order:88', {'id': 88, 'line_items': [{'sku': 'ABC'}, {'sku': 'ABC'}]}),
        'reconcile_sku': ('shopify-order:88:ABC', 'sku:ABC', {'sku': 'ABC', 'shopify_order_id': 'gid://shopify/Order/88'}),
    }
    source, resource, default_payload = defaults.get(kind, ('wrong', 'wrong', {}))
    job_id = ctx.db.enqueue_job(kind, source, default_payload if payload is None else payload, resource)
    with ctx.db._connect() as connection:
        connection.execute('UPDATE jobs SET attempts = ? WHERE id = ?', (attempts, job_id))
    return job_id


def run(ctx, **kwargs):
    return worker.process_one(ctx.db, ctx.shopify, ctx.meli, now=ctx.now, **kwargs)


def row(db, job_id):
    with db._connect() as connection:
        return dict(connection.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone())


@pytest.mark.parametrize('attempt,delay', [(1, 60), (2, 300), (3, 1800), (4, 7200)])
def test_failure_is_scheduled_at_the_required_delay(ctx, attempt, delay):
    job_id = enqueue(ctx, attempts=attempt - 1)
    ctx.meli.error = RetryableSyncError('offline')
    run(ctx)
    job = ctx.db.get_job(job_id)
    assert worker.retry_delay(attempt) == delay
    assert (job.status, job.attempts, job.available_at) == (
        'retry_wait', attempt, (ctx.now + timedelta(seconds=delay)).isoformat())
    assert row(ctx.db, job_id)['lease_until'] is None
    assert ctx.shopify.drafts == {}


def test_fifth_failure_publishes_review_before_terminal_state(ctx):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('still unavailable')
    def before_review():
        assert ctx.db.get_job(job_id).status == 'processing'

    ctx.shopify.before_review = before_review
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert 'still unavailable' in ctx.shopify.drafts['order:2001']
    assert ctx.db.get_review_link('order:2001') == 'gid://shopify/DraftOrder/99'


@pytest.mark.parametrize('error', [RetryableSyncError('Shopify unavailable'), ReviewRequiredError('auth', 'access denied', {})])
def test_failed_review_publication_retries_notice_without_rerunning_business_handler(ctx, error):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('original failure')
    ctx.shopify.review_error = error
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'retry_wait'
    assert ctx.meli.reads == 1
    ctx.shopify.review_error = None
    ctx.meli.error = None
    ctx.now += timedelta(hours=2)
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert ctx.meli.reads == 1
    assert ctx.shopify.orders == {}
    assert 'original failure' in ctx.shopify.drafts['order:2001']


def test_unknown_type_is_reviewed_without_calling_business_handlers(ctx):
    job_id = enqueue(ctx, 'wrong')
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert ctx.meli.reads == 0
    assert ctx.shopify.orders == {}
    assert 'product:worker-job-1' in ctx.shopify.drafts


@pytest.mark.parametrize('kind', ['import_meli_order', 'shopify_order', 'reconcile_sku'])
def test_explicit_routing_completes_one_job_with_real_handler_effects(ctx, kind):
    job_id = enqueue(ctx, kind)
    result = run(ctx)
    assert result['job_id'] == job_id
    assert ctx.db.get_job(job_id).status == 'completed'
    if kind == 'import_meli_order':
        assert ctx.shopify.quantity == 6
        assert ctx.db.get_order_link('2001') == 'gid://shopify/Order/77'
    if kind in {'import_meli_order', 'shopify_order'}:
        assert ctx.db.count('jobs') == 2
        assert ctx.db.get_job(2).status == 'pending'
        assert ctx.db.get_job(2).resource_key == 'sku:ABC'
    else:
        assert ctx.meli.writes == [('ABC', 8)]


@pytest.mark.parametrize('kind', ['import_meli_order', 'shopify_order', 'reconcile_sku'])
def test_handler_persisted_review_is_not_completed_or_published_twice(ctx, kind):
    job_id = enqueue(ctx, kind, payload={'id': 88, 'line_items': [{}]} if kind == 'shopify_order' else None)
    if kind == 'import_meli_order':
        ctx.meli.order = replace(ctx.meli.order, lines=[])
    elif kind == 'reconcile_sku':
        ctx.meli.listings = []
    notices = []
    ctx.shopify.before_review = lambda: notices.append(True)
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert len(notices) == 1
    if kind != 'import_meli_order':
        key = 'shopify-order:88' if kind == 'shopify_order' else 'sku:ABC'
        assert ('gid://shopify/Order/88', key) in ctx.shopify.order_reviews
        assert ctx.shopify.drafts == {}


def test_retry_exhaustion_for_reconciliation_marks_real_order(ctx):
    job_id = enqueue(ctx, 'reconcile_sku', attempts=4)
    ctx.shopify.quantity_error = RetryableSyncError('quantity unavailable')
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert ('gid://shopify/Order/88', 'sku:ABC') in ctx.shopify.order_reviews
    assert ctx.shopify.drafts == {}


@pytest.mark.parametrize('kind', ['import_meli_order', 'shopify_order', 'reconcile_sku'])
@pytest.mark.parametrize('invalid', [False, True])
def test_dry_run_returns_pending_without_mutations_or_consuming_attempts(ctx, kind, invalid):
    job_id = enqueue(ctx, kind, attempts=3,
                     payload={'id': 88, 'line_items': [{}]} if invalid and kind == 'shopify_order' else None)
    if invalid:
        ctx.meli.order = replace(ctx.meli.order, lines=[])
        ctx.meli.listings = []
    result = run(ctx, dry_run=True)
    assert result['dry_run'] is True
    assert (ctx.db.get_job(job_id).status, ctx.db.get_job(job_id).attempts) == ('pending', 3)
    assert row(ctx.db, job_id)['lease_until'] is None
    assert ctx.db.count('jobs') == 1
    assert ctx.db.count('order_links') == ctx.db.count('review_links') == 0
    assert ctx.shopify.quantity == 8
    assert ctx.shopify.orders == ctx.shopify.drafts == ctx.shopify.order_reviews == {}
    assert ctx.meli.writes == []


def test_dry_run_read_failure_returns_pending_without_failure_budget(ctx):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('offline')
    result = run(ctx, dry_run=True)
    assert result['status'] == 'retry_wait'
    assert (ctx.db.get_job(job_id).status, ctx.db.get_job(job_id).attempts) == ('pending', 4)
    assert row(ctx.db, job_id)['lease_until'] is None
    assert ctx.shopify.drafts == {}


def test_dry_run_does_not_increment_attempt_even_during_inspection(ctx, monkeypatch):
    job_id = enqueue(ctx, attempts=4)
    original = ctx.meli.get_order

    def inspect(order_id):
        assert ctx.db.get_job(job_id).attempts == 4
        assert ctx.db.get_job(job_id).status == 'processing'
        return original(order_id)

    monkeypatch.setattr(ctx.meli, 'get_order', inspect)
    run(ctx, dry_run=True)
    assert ctx.db.get_job(job_id).attempts == 4


@pytest.mark.parametrize('selected_expired', [False, True])
def test_dry_run_preserves_entire_unrelated_expired_job_row(ctx, selected_expired):
    selected = enqueue(ctx, attempts=2)
    unrelated = enqueue(ctx, 'shopify_order', attempts=3)
    expired_at = (ctx.now - timedelta(minutes=1)).isoformat()
    with ctx.db._connect() as connection:
        connection.execute(
            'UPDATE jobs SET status = ?, lease_until = ?, available_at = ? WHERE id = ?',
            ('processing' if selected_expired else 'pending', expired_at if selected_expired else None,
             (ctx.now - timedelta(hours=2)).isoformat(), selected),
        )
        connection.execute(
            """UPDATE jobs SET status = 'processing', lease_until = ?, available_at = ?,
               last_error = 'original unrelated failure', updated_at = ? WHERE id = ?""",
            (expired_at, (ctx.now - timedelta(hours=1)).isoformat(), expired_at, unrelated),
        )
    before = row(ctx.db, unrelated)
    result = run(ctx, dry_run=True)
    assert result['job_id'] == selected
    assert row(ctx.db, unrelated) == before
    assert ctx.db.get_job(selected).status == 'pending'
    assert ctx.db.get_job(selected).attempts == 2
    assert row(ctx.db, selected)['lease_until'] is None
    with ctx.db._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM sync_logs WHERE job_id = ?', (unrelated,)).fetchone()[0] == 0


def test_dry_run_with_pending_review_notice_does_not_publish_or_restart_import(ctx):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('original failure')
    ctx.shopify.review_error = RetryableSyncError('Shopify down')
    run(ctx)
    ctx.now += timedelta(hours=2)
    ctx.shopify.review_error = None
    result = run(ctx, dry_run=True)
    assert result['status'] == 'needs_review'
    assert ctx.db.get_job(job_id).status == 'pending'
    assert ctx.db.get_job(job_id).attempts == 5
    assert ctx.meli.reads == 1
    assert ctx.shopify.drafts == {}
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert ctx.meli.reads == 1


def test_unexpired_job_lease_is_not_reclaimed_and_expired_lease_is_recovered(ctx):
    job_id = enqueue(ctx)
    ctx.db.claim_next_job(ctx.now)
    assert run(ctx) is None
    ctx.now += timedelta(minutes=5)
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'completed'
    assert ctx.db.get_job(job_id).attempts == 2


def test_unexpected_handler_error_releases_job_for_retry(ctx):
    job_id = enqueue(ctx, payload={})
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'retry_wait'
    assert row(ctx.db, job_id)['lease_until'] is None


def test_transitions_are_logged_without_secrets(ctx, monkeypatch, caplog):
    monkeypatch.setenv('SHOPIFY_ACCESS_TOKEN', 'sensitive-fixture')
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('token=sensitive-fixture Authorization: Bearer oauth-fixture')
    run(ctx)
    with ctx.db._connect() as connection:
        messages = [r[0] for r in connection.execute('SELECT message FROM sync_logs WHERE job_id = ?', (job_id,))]
    assert any('processing' in message for message in messages)
    assert any('needs_review' in message for message in messages)
    emitted = json.dumps(messages) + caplog.text + str(ctx.db.get_job(job_id).last_error) + str(ctx.shopify.drafts)
    assert 'sensitive-fixture' not in emitted
    assert 'oauth-fixture' not in emitted


def test_daily_and_queue_share_lock_while_receiver_can_still_enqueue(ctx):
    job_id = enqueue(ctx)

    def during_daily():
        with pytest.raises(worker.WorkerBusy):
            run(ctx)
        with pytest.raises(worker.WorkerBusy):
            worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now)
        Database(ctx.db.path).enqueue_job('wrong', 'receiver-still-writable', {}, 'other')

    ctx.meli.on_search = during_daily
    result = worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now)
    assert result.status == 'completed'
    assert ctx.db.get_job(job_id).status == 'pending'
    assert ctx.db.count('jobs') == 2
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'completed'


def test_daily_dry_run_leaves_checkpoint_and_business_data_unchanged(ctx):
    ctx.meli.paid_orders = ['2001']
    result = worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now, dry_run=True)
    assert result.status == 'dry_run'
    assert ctx.db.count('jobs') == 0
    assert ctx.db.get_checkpoint('daily_orders_completed_at') is None
    assert ctx.shopify.orders == {}
    assert ctx.meli.writes == []


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('paid_orders', [['2001'], []])
def test_daily_blocks_pending_review_publication_before_import_or_catalog_work(ctx, dry_run, paid_orders, monkeypatch):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('original failure')
    ctx.shopify.review_error = RetryableSyncError('Shopify unavailable')
    run(ctx)
    before = row(ctx.db, job_id)
    notice = ctx.db.get_checkpoint(f'worker_review:{job_id}')
    assert before['status'] == 'retry_wait'
    assert notice is not None

    ctx.meli.error = None
    ctx.shopify.review_error = None
    ctx.meli.paid_orders = paid_orders
    catalog_reads = []
    original_catalog = ctx.shopify.list_all_variants

    def read_catalog():
        catalog_reads.append(True)
        return original_catalog()

    monkeypatch.setattr(ctx.shopify, 'list_all_variants', read_catalog)
    result = worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now, dry_run=dry_run)
    assert result.status == 'needs_review'
    assert result.review_keys == ['order:2001']
    assert row(ctx.db, job_id) == before
    assert ctx.db.get_checkpoint(f'worker_review:{job_id}') == notice
    assert ctx.db.get_checkpoint('daily_orders_completed_at') is None
    assert ctx.meli.reads == 1
    assert ctx.shopify.orders == {}
    assert ctx.meli.writes == []
    assert catalog_reads == []


def test_daily_keeps_published_import_review_blocking_until_explicit_retry(ctx):
    job_id = enqueue(ctx, attempts=4)
    ctx.meli.error = RetryableSyncError('original failure')
    ctx.shopify.review_error = RetryableSyncError('Shopify unavailable')
    run(ctx)
    ctx.now += timedelta(hours=2)
    ctx.meli.error = None
    ctx.shopify.review_error = None
    run(ctx)
    assert ctx.db.get_job(job_id).status == 'needs_review'
    assert ctx.db.get_checkpoint(f'worker_review:{job_id}') is None
    ctx.meli.paid_orders = ['2001']
    result = worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now)
    assert result.status == 'needs_review'
    assert ctx.meli.reads == 1
    assert ctx.shopify.orders == {}
    assert ctx.db.get_job(job_id).status == 'needs_review'

    assert worker.retry_review(ctx.db, job_id)
    result = worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now)
    assert result.status == 'completed'
    assert ctx.db.get_job(job_id).status == 'completed'
    assert ctx.shopify.orders == {'2001': 'gid://shopify/Order/77'}


def test_lock_is_released_after_daily_failure(ctx):
    ctx.meli.paid_orders = ['2001']
    ctx.meli.error = RetryableSyncError('offline')
    with pytest.raises(RetryableSyncError):
        worker.process_daily(ctx.db, ctx.shopify, ctx.meli, now=ctx.now)
    ctx.meli.error = None
    run(ctx)
    assert ctx.db.get_job(1).status == 'completed'


def test_worker_lock_is_exclusive_across_processes_and_releases_on_crash(ctx):
    script = (
        'import sys, signal; from stock_sync.db import Database; from worker import worker_lock; '
        'db = Database(sys.argv[1]); '
        'lock = worker_lock(db); lock.__enter__(); print("locked", flush=True); signal.pause()'
    )
    child = subprocess.Popen([sys.executable, '-u', '-c', script, ctx.db.path],
                             cwd=Path(worker.__file__).parent, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(worker.WorkerBusy):
            with worker.worker_lock(ctx.db):
                pass
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with worker.worker_lock(ctx.db):
        pass


def test_retry_command_resets_only_selected_review_job(ctx, monkeypatch, capsys):
    first = enqueue(ctx, attempts=5)
    second = enqueue(ctx, 'shopify_order')
    ctx.db.needs_review(first, 'order:2001', 'repair needed')
    monkeypatch.setenv('STOCK_SYNC_DATABASE', ctx.db.path)
    assert worker.main(['retry', str(first)]) == 0
    assert (ctx.db.get_job(first).status, ctx.db.get_job(first).attempts, ctx.db.get_job(first).last_error) == ('pending', 0, None)
    assert ctx.db.get_job(second).status == 'pending'
    assert worker.main(['retry', str(second)]) != 0
    assert worker.main(['retry', '999']) != 0


def test_list_review_outputs_required_fields_without_api_credentials(ctx, monkeypatch, capsys):
    job_id = enqueue(ctx, attempts=5)
    ctx.db.needs_review(job_id, 'order:2001', 'repair needed')
    enqueue(ctx, 'shopify_order')
    monkeypatch.setenv('STOCK_SYNC_DATABASE', ctx.db.path)
    assert worker.main(['list-review']) == 0
    output = capsys.readouterr().out
    for expected in [str(job_id), 'import_meli_order', 'meli-order:2001', '5', 'repair needed']:
        assert expected in output
    assert 'shopify-order:88' not in output


def test_list_review_redacts_secrets_and_preserves_parseable_json(ctx, monkeypatch, capsys):
    job_id = enqueue(ctx)
    ctx.db.needs_review(job_id, 'order:2001', 'access_token=temporary-credential')
    monkeypatch.setenv('STOCK_SYNC_DATABASE', ctx.db.path)
    assert worker.main(['list-review']) == 0
    output = capsys.readouterr().out
    assert 'temporary-credential' not in output
    assert json.loads(output)['last_error'] == 'access_token=[REDACTED]'


def test_cli_daily_runs_inspection_and_reports_lock_contention(ctx, monkeypatch, capsys):
    monkeypatch.setenv('STOCK_SYNC_DATABASE', ctx.db.path)
    monkeypatch.setattr(worker.Settings, 'from_env', lambda: None)
    monkeypatch.setattr(worker, 'ShopifyClient', lambda settings: ctx.shopify)
    monkeypatch.setattr(worker, 'MeliClient', lambda settings: ctx.meli)
    assert worker.main(['daily', '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['planned_updates'] == [
        {'sku': 'ABC', 'item_id': 'MLC1', 'variation_id': None, 'quantity': 8}]
    assert ctx.meli.writes == []
    with worker.worker_lock(ctx.db):
        assert worker.main(['daily']) == 2
    assert 'active' in capsys.readouterr().out


def test_migrate_and_once_cli_can_run_offline(tmp_path):
    environment = {**os.environ, 'STOCK_SYNC_DATABASE': str(tmp_path / 'cli.db')}
    for key in ('SHOPIFY_SHOP_URL', 'SHOPIFY_ACCESS_TOKEN', 'SHOPIFY_WEBHOOK_SECRET', 'MELI_APP_ID',
                'MELI_CLIENT_SECRET', 'MELI_EXPECTED_SELLER_ID', 'MELI_WEBHOOK_TOKEN'):
        environment[key] = 'offline-fixture'
    script = str(Path(worker.__file__))
    for arguments in (['migrate'], ['once', '--dry-run'], ['list-review']):
        result = subprocess.run([sys.executable, script, *arguments], env=environment, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    assert Database(environment['STOCK_SYNC_DATABASE']).count('jobs') == 0


def test_run_stops_cleanly_on_sigterm_and_releases_lock(ctx, monkeypatch):
    first = enqueue(ctx)
    enqueue(ctx, 'shopify_order')
    real_get_order = ctx.meli.get_order

    def stopping_order(order_id):
        os.kill(os.getpid(), signal.SIGTERM)
        return real_get_order(order_id)

    monkeypatch.setattr(ctx.meli, 'get_order', stopping_order)
    old_handler = signal.getsignal(signal.SIGTERM)
    worker.run_loop(ctx.db, ctx.shopify, ctx.meli)
    assert signal.getsignal(signal.SIGTERM) == old_handler
    assert ctx.db.get_job(first).status == 'completed'
    assert ctx.db.get_job(2).status == 'pending'
    with worker.worker_lock(ctx.db):
        pass
