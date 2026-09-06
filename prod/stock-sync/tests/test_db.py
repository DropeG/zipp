from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest


def set_available_at(db, source_key: str, available_at: str) -> None:
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "UPDATE jobs SET available_at = ? WHERE source_key = ?", (available_at, source_key)
        )


def test_duplicate_source_key_creates_one_job(db):
    first = db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")
    second = db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")

    assert first == second
    assert db.count("jobs") == 1


def test_claim_is_atomic_and_respects_resource_lock(db, clock):
    db.enqueue_job("reconcile_sku", "order:1:ABC", {"sku": "ABC"}, "sku:ABC")
    db.enqueue_job("reconcile_sku", "order:2:ABC", {"sku": "ABC"}, "sku:ABC")
    set_available_at(db, "order:1:ABC", "2026-09-06T00:00:00+00:00")
    set_available_at(db, "order:2:ABC", "2026-09-06T00:00:00+00:00")

    first = db.claim_next_job(clock.now())

    assert first.resource_key == "sku:ABC"
    assert first.status == "processing"
    assert first.attempts == 1
    assert db.claim_next_job(clock.now()) is None


def test_claim_does_not_lease_a_pending_job_before_it_is_due(db, clock):
    db.enqueue_job("reconcile_sku", "order:future:ABC", {"sku": "ABC"}, "sku:ABC")
    set_available_at(db, "order:future:ABC", "2026-09-06T00:05:00+00:00")

    assert db.claim_next_job(clock.now()) is None


def test_retry_makes_job_available_at_requested_time(db, clock):
    db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")
    set_available_at(db, "meli:123", "2026-09-06T00:00:00+00:00")
    job = db.claim_next_job(clock.now())

    db.retry_job(job.id, "timeout", clock.now(), delay_seconds=300)

    retried = db.get_job(job.id)
    assert retried.status == "retry_wait"
    assert retried.available_at == "2026-09-06T00:05:00+00:00"
    assert retried.last_error == "timeout"


def test_expired_lease_becomes_retry_wait_before_claiming_next_job(db, clock):
    first_id = db.enqueue_job("reconcile_sku", "order:1:ABC", {"sku": "ABC"}, "sku:ABC")
    set_available_at(db, "order:1:ABC", "2026-09-06T00:00:00+00:00")
    db.claim_next_job(clock.now())

    claimed = db.claim_next_job(clock.now() + timedelta(minutes=6))

    assert claimed.id == first_id
    assert claimed.attempts == 2


def test_terminal_and_link_operations_persist_data(db, clock):
    job_id = db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")
    set_available_at(db, "meli:123", "2026-09-06T00:00:00+00:00")
    db.claim_next_job(clock.now())

    db.complete_job(job_id)
    db.link_order("123", "shopify-456")
    db.link_review("review:123", "123")
    db.needs_review(job_id, "review:123", "manual check")

    assert db.get_job(job_id).status == "needs_review"
    assert db.get_job(job_id).last_error == "manual check"
    assert db.get_order_link("123") == "shopify-456"
    assert db.get_review_link("review:123") == "123"


def test_checkpoint_round_trips(db):
    assert db.get_checkpoint("meli-orders") is None

    db.set_checkpoint("meli-orders", "cursor-123")

    assert db.get_checkpoint("meli-orders") == "cursor-123"


def test_settings_require_secrets_and_apply_defaults(monkeypatch):
    from stock_sync.config import Settings

    required = {
        "SHOPIFY_SHOP_URL": "https://example.myshopify.com",
        "SHOPIFY_ACCESS_TOKEN": "shopify-token",
        "SHOPIFY_WEBHOOK_SECRET": "webhook-secret",
        "MELI_APP_ID": "meli-app-id",
        "MELI_CLIENT_SECRET": "meli-secret",
        "MELI_EXPECTED_SELLER_ID": "123",
        "MELI_WEBHOOK_TOKEN": "meli-webhook-token",
    }
    for name in required:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SHOPIFY_API_VERSION", raising=False)
    monkeypatch.delenv("MAX_WEBHOOK_BYTES", raising=False)
    monkeypatch.delenv("STOCK_SYNC_DATABASE", raising=False)

    with pytest.raises(ValueError, match="SHOPIFY_SHOP_URL"):
        Settings.from_env()

    for name, value in required.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.shopify_api_version == "2026-07"
    assert settings.max_webhook_bytes == 1048576
    assert settings.database_path == "prod/stock-sync/data/stock_sync.db"
