"""Regression scenarios exercise real handlers, SQLite and Shopify requests.

Only external network boundaries are replaced with stateful provider fixtures.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import worker
from stock_sync.db import Database
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.handlers import handle_import_meli_order, handle_reconcile_sku, handle_shopify_order
from stock_sync.shopify import ShopifyClient
from test_import_handler import FakeMeli, line
from test_reconcile_handler import FakeMeli as ReconcileMeli
from test_shopify import variant_page


ORDER_ID = "gid://shopify/Order/77"
DRAFT_ID = "gid://shopify/DraftOrder/91"


class Provider:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.drafts = {}
        self.order_search_visible = True
        self.draft_search_visible = True
        self.lose_create_response = False
        self.quantity = 7
        self.tracked = True
        self.variant_missing = False
        self.create_count = 0

    def execute(self, operation, query, variables):
        self.calls.append((operation, deepcopy(variables)))
        if operation in {"find_variants", "list_all_variants"}:
            page = variant_page("ABC", 11)
            node = page["productVariants"]["nodes"][0]
            node["inventoryQuantity"] = self.quantity
            node["inventoryItem"]["tracked"] = self.tracked
            if self.variant_missing:
                page["productVariants"]["nodes"] = []
            return page
        if operation == "find_imported_order":
            return {"orders": {"nodes": list(self.orders.values()) if self.order_search_visible else []}}
        if operation == "create_imported_order":
            self.create_count += 1
            self.quantity -= sum(line["quantity"] for line in variables["order"]["lineItems"])
            self.orders[ORDER_ID] = {"id": ORDER_ID, "note": "Original note", "tags": variables["order"]["tags"]}
            if self.lose_create_response:
                raise RetryableSyncError("response lost after acceptance")
            return {"orderCreate": {"order": {"id": ORDER_ID}, "userErrors": []}}
        if operation == "get_order_skus":
            return {"order": {"id": ORDER_ID, "lineItems": {
                "nodes": [{"sku": "ABC", "title": "Widget", "quantity": 2}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        if operation == "find_order":
            return {"node": deepcopy(self.orders.get(variables["id"]))}
        if operation in {"mark_order_review", "resolve_order_review"}:
            data = variables["input"]
            self.orders[data["id"]].update(deepcopy(data))
            return {"orderUpdate": {"order": {"id": data["id"]}, "userErrors": []}}
        if operation == "find_review":
            stable_tag = variables["query"].removeprefix("tag:")
            return {"draftOrders": {"nodes": [{**deepcopy(d), "note2": d["note"]} for d in self.drafts.values()
                if stable_tag in d["tags"] and self.draft_search_visible]}}
        if operation == "get_review":
            draft = self.drafts.get(variables["id"])
            return {"node": {**deepcopy(draft), "note2": draft["note"]} if draft else None}
        if operation == "create_review":
            draft_id = DRAFT_ID if not self.drafts else f"gid://shopify/DraftOrder/{91 + len(self.drafts)}"
            self.drafts[draft_id] = {"id": draft_id, **deepcopy(variables["input"])}
            return {"draftOrderCreate": {"draftOrder": {"id": draft_id}, "userErrors": []}}
        if operation in {"update_review", "resolve_review"}:
            self.drafts[variables["id"]].update(deepcopy(variables["input"]))
            return {"draftOrderUpdate": {"draftOrder": {"id": variables["id"]}, "userErrors": []}}
        raise AssertionError(f"Unexpected external operation: {operation}")


def import_job(db, notice="first"):
    id = db.enqueue_job("import_meli_order", f"meli-notice:2001:{notice}", {"order_id": "2001"}, "order:2001")
    return db.get_job(id)


def reconcile_job(db, sku="ABC"):
    id = db.enqueue_job("reconcile_sku", f"shopify-order:77:{sku}",
                        {"sku": sku, "shopify_order_id": ORDER_ID}, f"sku:{sku}")
    return db.get_job(id)


def setup_order(provider):
    provider.orders[ORDER_ID] = {"id": ORDER_ID, "tags": ["original"], "note": "Original note"}


def test_unpaid_then_paid_then_duplicate_notifications_import_once(db):
    provider, meli = Provider(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    meli.order = replace(meli.order, status="payment_required")
    assert handle_import_meli_order(import_job(db), db, shopify, meli)["status"] == "skipped"
    meli.order = replace(meli.order, status="paid")
    handle_import_meli_order(import_job(db, "paid"), db, shopify, meli)
    handle_import_meli_order(import_job(db, "paid-again"), db, shopify, meli)
    assert provider.create_count == 1
    assert provider.quantity == 5


def test_uncertain_create_survives_restart_and_manual_retry_until_search_reconciles(db):
    provider, meli = Provider(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    provider.lose_create_response = True
    provider.order_search_visible = False
    job = import_job(db)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "retry_wait"
    # Reopen the database and permit future calls to succeed: a missing search
    # result must still never authorize a second order creation.
    db = Database(db.path)
    provider.lose_create_response = False
    now += timedelta(minutes=2)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "needs_review"
    assert provider.create_count == 1
    assert worker.retry_review(db, job.id)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "needs_review"
    assert provider.create_count == 1
    provider.order_search_visible = True
    assert worker.retry_review(db, job.id)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "imported"
    assert db.get_order_link("2001") == ORDER_ID
    assert provider.create_count == 1 and provider.quantity == 5
    assert ORDER_ID in provider.drafts[DRAFT_ID]["note"]
    assert "meli-review-resolved" in provider.drafts[DRAFT_ID]["tags"]


def test_existing_link_uses_real_shopify_lines_before_historical_meli_validation(db):
    provider, meli = Provider(), FakeMeli()
    setup_order(provider)
    db.link_order("2001", ORDER_ID)
    meli.error = ReviewRequiredError("order:2001", "historical price malformed", {})
    result = handle_import_meli_order(import_job(db), db, ShopifyClient(None, transport=provider), meli)
    assert result["status"] == "imported"
    assert meli.requested_ids == []
    assert provider.drafts == {} and provider.create_count == 0
    with db._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE job_type = 'reconcile_sku'").fetchone()[0] == 1


def test_linked_order_later_problem_marks_real_order_without_import_draft(db):
    class MissingSkuProvider(Provider):
        def execute(self, operation, query, variables):
            result = super().execute(operation, query, variables)
            if operation == "get_order_skus":
                result["order"]["lineItems"]["nodes"][0]["sku"] = ""
            return result
    provider, meli = MissingSkuProvider(), FakeMeli()
    setup_order(provider)
    db.link_order("2001", ORDER_ID)
    meli.error = ReviewRequiredError("order:2001", "historical line malformed", {})
    result = handle_import_meli_order(import_job(db), db, ShopifyClient(None, transport=provider), meli)
    assert result["status"] == "needs_review"
    assert provider.drafts == {}
    assert "meli-needs-review" in provider.orders[ORDER_ID]["tags"]


def test_import_review_reuses_persisted_draft_then_resolves_with_order_id(db):
    provider, meli = Provider(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    job = replace(import_job(db), attempts=3)
    meli.order = replace(meli.order, lines=[line("")])
    handle_import_meli_order(job, db, shopify, meli)
    provider.draft_search_visible = False
    handle_import_meli_order(job, db, shopify, meli)
    assert len(provider.drafts) == 1
    note = provider.drafts[DRAFT_ID]["note"]
    assert all(value in note for value in ["2001", "Widget", "MLC1", "2", "3"])
    meli.order = replace(meli.order, lines=[line()])
    handle_import_meli_order(job, db, shopify, meli)
    assert "meli-needs-review" not in provider.drafts[DRAFT_ID]["tags"]
    assert "meli-review-resolved" in provider.drafts[DRAFT_ID]["tags"]
    assert ORDER_ID in provider.drafts[DRAFT_ID]["note"]
    assert "Widget" in provider.drafts[DRAFT_ID]["note"]


def test_queued_untracked_quantity_never_reaches_meli_write(db):
    provider, meli = Provider(), ReconcileMeli([])
    setup_order(provider)
    provider.tracked = False
    result = handle_reconcile_sku(reconcile_job(db), db, ShopifyClient(None, transport=provider), meli)
    assert result["status"] == "needs_review"
    assert meli.quantity_writes == []
    assert "tracked" in provider.orders[ORDER_ID]["note"]


def test_successful_sku_retry_resolves_only_its_problem_on_real_order(db):
    provider, meli = Provider(), ReconcileMeli([])
    setup_order(provider)
    shopify = ShopifyClient(None, transport=provider)
    job = reconcile_job(db)
    meli.list_error = ReviewRequiredError("sku:ABC", "listing temporarily unmapped", {})
    handle_reconcile_sku(job, db, shopify, meli)
    bad = db.enqueue_job("shopify_order", "shopify-order:77", {"id": 77, "line_items": [{}]}, "order:77")
    handle_shopify_order(db.get_job(bad), db, shopify)
    meli.list_error = None
    handle_reconcile_sku(job, db, shopify, meli)
    tags = provider.orders[ORDER_ID]["tags"]
    assert "meli-review-sku:ABC" not in tags
    assert "meli-review-shopify-order:77" in tags and "meli-needs-review" in tags
    repaired = replace(db.get_job(bad), payload={"id": 77, "line_items": [{"sku": "ABC"}]})
    handle_shopify_order(repaired, db, shopify)
    assert "meli-needs-review" not in provider.orders[ORDER_ID]["tags"]
    assert "original" in provider.orders[ORDER_ID]["tags"]
    assert "Original note" in provider.orders[ORDER_ID]["note"]
    meli.list_error = ReviewRequiredError("sku:ABC", "mapping broke again", {})
    handle_reconcile_sku(job, db, shopify, meli)
    assert "meli-review-resolved" not in provider.orders[ORDER_ID]["tags"]
    assert "meli-needs-review" in provider.orders[ORDER_ID]["tags"]


@pytest.mark.parametrize("meli_quantity", [0, 10])
def test_negative_stock_writes_safe_zero_and_keeps_shortage_review(db, meli_quantity):
    provider, meli = Provider(), ReconcileMeli([])
    setup_order(provider)
    provider.quantity = -2
    meli.listings = [replace(meli.listings[0], available_quantity=meli_quantity)]
    shopify = ShopifyClient(None, transport=provider)
    job = reconcile_job(db)
    result = handle_reconcile_sku(job, db, shopify, meli)
    assert result["status"] == "needs_review"
    assert meli.listings[0].available_quantity == 0
    assert "shortage" in provider.orders[ORDER_ID]["note"].lower()
    assert db.get_job(job.id).status == "needs_review"
    provider.quantity = 2
    handle_reconcile_sku(job, db, shopify, meli)
    assert "meli-needs-review" not in provider.orders[ORDER_ID]["tags"]


def test_confirmed_create_rejection_can_be_repaired_without_uncertain_retry(db):
    class RejectOnce(Provider):
        rejected = False

        def execute(self, operation, query, variables):
            if operation == "create_imported_order" and not self.rejected:
                self.rejected = True
                return {"orderCreate": {"order": None, "userErrors": [{"field": ["lineItems"], "message": "invalid price"}]}}
            return super().execute(operation, query, variables)

    provider, meli = RejectOnce(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    job = import_job(db)
    assert handle_import_meli_order(job, db, shopify, meli)["status"] == "needs_review"
    assert handle_import_meli_order(job, db, shopify, meli)["status"] == "imported"
    assert provider.create_count == 1 and provider.quantity == 5
    assert "meli-review-resolved" in provider.drafts[DRAFT_ID]["tags"]


def test_missing_created_order_id_stays_uncertain_and_never_links_null(db):
    class MissingId(Provider):
        def execute(self, operation, query, variables):
            result = super().execute(operation, query, variables)
            if operation == "create_imported_order":
                result["orderCreate"]["order"]["id"] = None
            return result
    provider, meli = MissingId(), FakeMeli()
    provider.order_search_visible = False
    shopify = ShopifyClient(None, transport=provider)
    job = import_job(db)
    assert handle_import_meli_order(job, db, shopify, meli)["status"] == "needs_review"
    assert db.get_order_link("2001") is None
    assert handle_import_meli_order(job, db, shopify, meli)["status"] == "needs_review"
    assert provider.create_count == 1


def test_worker_failure_after_link_uses_real_order_and_resolves_on_retry(db):
    class FailingOrderRead(Provider):
        fail = True

        def execute(self, operation, query, variables):
            if operation == "get_order_skus" and self.fail:
                raise RetryableSyncError("cannot read order lines")
            return super().execute(operation, query, variables)
    provider, meli = FailingOrderRead(), FakeMeli()
    setup_order(provider)
    db.link_order("2001", ORDER_ID)
    shopify = ShopifyClient(None, transport=provider)
    job = import_job(db)
    with db._connect() as connection:
        connection.execute("UPDATE jobs SET attempts = 4 WHERE id = ?", (job.id,))
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "needs_review"
    assert provider.drafts == {}
    assert "meli-needs-review" in provider.orders[ORDER_ID]["tags"]
    provider.fail = False
    worker.retry_review(db, job.id)
    assert worker.process_one(db, shopify, meli, now=now)["status"] == "imported"
    assert "meli-needs-review" not in provider.orders[ORDER_ID]["tags"]


def test_worker_publication_reuses_local_draft_after_search_miss(db):
    provider, meli = Provider(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    job = import_job(db)
    meli.error = RetryableSyncError("Meli unavailable")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    for _ in range(2):
        with db._connect() as connection:
            connection.execute("UPDATE jobs SET attempts = 4, status = 'pending' WHERE id = ?", (job.id,))
        assert worker.process_one(db, shopify, meli, now=now)["status"] == "needs_review"
        provider.draft_search_visible = False
    assert len(provider.drafts) == 1


def test_malformed_cancelled_order_does_not_block_daily_recovery(db):
    from stock_sync.daily import run_daily
    from stock_sync.meli import MeliClient
    from types import SimpleNamespace
    from test_daily import Shopify

    class Transport:
        def request(self, method, path, **kwargs):
            if path == "/orders/search":
                return {"results": [{"id": 2001}], "paging": {"offset": 0, "total": 1}}
            if path == "/orders/2001":
                return {"id": 2001, "seller": {"id": 100}, "status": "cancelled", "order_items": "broken"}
            if path == "/users/100/items/search":
                return {"results": [], "paging": {"total": 0}}
            raise AssertionError(path)

    shopify = Shopify([])
    shopify.catalog = []
    meli = MeliClient(SimpleNamespace(meli_expected_seller_id="100"), transport=Transport())
    result = run_daily(db, shopify, meli, datetime.now(timezone.utc))
    assert result.status == "completed"
    assert db.get_checkpoint("daily_orders_completed_at") is not None
    assert shopify.reviews == {}


def test_daily_product_review_reuses_persisted_link_and_resolves_after_repair(db):
    from stock_sync.daily import run_daily
    from test_daily import Meli

    provider, meli = Provider(), Meli([])
    shopify = ShopifyClient(None, transport=provider)
    provider.variant_missing = True
    now = datetime.now(timezone.utc)
    run_daily(db, shopify, meli, now)
    provider.draft_search_visible = False
    run_daily(db, shopify, meli, now)
    assert len(provider.drafts) == 1
    provider.variant_missing = False
    run_daily(db, shopify, meli, now)
    assert "meli-needs-review" not in provider.drafts[DRAFT_ID]["tags"]


def test_later_successful_import_clears_obsolete_import_review_jobs(db):
    provider, meli = Provider(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    first = import_job(db)
    meli.order = replace(meli.order, lines=[line("")])
    handle_import_meli_order(first, db, shopify, meli)
    meli.order = replace(meli.order, lines=[line()])
    handle_import_meli_order(import_job(db, "repaired"), db, shopify, meli)
    assert db.get_job(first.id).status == "completed"
    assert db.get_job(first.id).last_error is None


def test_insufficient_stock_import_records_sale_once_then_reviews_shortage(db):
    provider, meli = Provider(), FakeMeli()
    provider.quantity = 1
    shopify = ShopifyClient(None, transport=provider)
    job = import_job(db)
    handle_import_meli_order(job, db, shopify, meli)
    stock = ReconcileMeli([])
    with db._connect() as connection:
        queued_id = connection.execute("SELECT id FROM jobs WHERE job_type = 'reconcile_sku'").fetchone()[0]
    result = handle_reconcile_sku(db.get_job(queued_id), db, shopify, stock)
    assert result["status"] == "needs_review"
    assert provider.create_count == 1 and provider.quantity == -1
    assert stock.listings[0].available_quantity == 0
    assert "meli-needs-review" in provider.orders[ORDER_ID]["tags"]


def test_malformed_direct_review_response_never_falls_back_to_create(db):
    class MalformedReview(Provider):
        def execute(self, operation, query, variables):
            if operation == "get_review":
                return {}
            return super().execute(operation, query, variables)
    provider, meli = MalformedReview(), FakeMeli()
    shopify = ShopifyClient(None, transport=provider)
    meli.order = replace(meli.order, lines=[line("")])
    job = import_job(db)
    handle_import_meli_order(job, db, shopify, meli)
    provider.draft_search_visible = False
    with pytest.raises(ReviewRequiredError):
        handle_import_meli_order(job, db, shopify, meli)
    assert len(provider.drafts) == 1


def test_linked_order_reconciliation_reads_every_line_page(db):
    class TwoPages(Provider):
        def execute(self, operation, query, variables):
            result = super().execute(operation, query, variables)
            if operation == "get_order_skus":
                page = result["order"]["lineItems"]
                if variables["after"] is None:
                    page["pageInfo"] = {"hasNextPage": True, "endCursor": "second"}
                else:
                    page["nodes"][0]["sku"] = "XYZ"
            return result
    provider = TwoPages()
    setup_order(provider)
    db.link_order("2001", ORDER_ID)
    handle_import_meli_order(import_job(db), db, ShopifyClient(None, transport=provider), FakeMeli())
    with db._connect() as connection:
        assert {row[0] for row in connection.execute("SELECT source_key FROM jobs WHERE job_type = 'reconcile_sku'")} == {
            "shopify-order:77:ABC", "shopify-order:77:XYZ"}


def test_resolution_distinguishes_numeric_sku_from_order_problem(db):
    from stock_sync.reviews import mark_order, resolve_order

    provider = Provider()
    setup_order(provider)
    shopify = ShopifyClient(None, transport=provider)
    mark_order(db, shopify, ORDER_ID, "sku:77", "SKU 77 shortage")
    mark_order(db, shopify, ORDER_ID, "shopify-order:77", "Missing line SKU")
    resolve_order(db, shopify, ORDER_ID, "sku:77")
    assert "meli-needs-review" in provider.orders[ORDER_ID]["tags"]
    resolve_order(db, shopify, ORDER_ID, "shopify-order:77")
    assert "meli-needs-review" not in provider.orders[ORDER_ID]["tags"]
