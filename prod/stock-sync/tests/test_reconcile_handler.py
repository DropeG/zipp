from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.handlers import (
    handle_import_meli_order,
    handle_reconcile_sku,
    handle_shopify_order,
)
from stock_sync.models import MeliListing, MeliOrder, MeliOrderLine, ShopifyVariant


class FakeShopify:
    def __init__(self, calls):
        self.calls = calls
        self.quantity = 8
        self.quantity_error = None
        self.review_error = None
        self.order_reviews = {}
        self.draft_reviews = {}
        self.inventory_adjustments = []
        self.imported_id = None

    def get_available_quantity(self, sku):
        self.calls.append(("shopify-read", sku))
        if self.quantity_error:
            raise self.quantity_error
        return self.quantity

    def mark_order_review(self, order_id, review_key, note):
        if self.review_error:
            raise self.review_error
        self.order_reviews[(order_id, review_key)] = note

    def create_or_update_review(self, review_key, note):
        if self.review_error:
            raise self.review_error
        self.draft_reviews[review_key] = note
        return "gid://shopify/DraftOrder/99"

    def find_variants_by_skus(self, skus):
        return {sku: [ShopifyVariant("gid://shopify/ProductVariant/1", sku, self.quantity, True)] for sku in skus}

    def find_imported_order(self, order_id):
        return self.imported_id

    def create_imported_order(self, order, variants):
        self.quantity -= sum(line.quantity for line in order.lines)
        self.imported_id = "gid://shopify/Order/77"
        return self.imported_id


class FakeMeli:
    def __init__(self, calls):
        self.calls = calls
        self.listings = [MeliListing("MLC1", None, "ABC", 10)]
        self.list_error = None
        self.write_error = None
        self.on_list = None
        self.quantity_writes = []

    def list_all_listings(self):
        self.calls.append(("meli-list",))
        if self.list_error:
            raise self.list_error
        if self.on_list:
            self.on_list()
        return self.listings

    def set_available_quantity(self, listing, quantity):
        self.calls.append(("meli-write", listing.sku, quantity))
        if self.write_error:
            raise self.write_error
        self.quantity_writes.append((listing, quantity))
        self.listings = [replace(item, available_quantity=quantity) if item == listing else item for item in self.listings]

    def get_order(self, order_id):
        return MeliOrder(order_id, "100", "paid", "2026-09-06T12:00:00+00:00", [
            MeliOrderLine("MLC1", None, "Widget", 2, "1000", "CLP", "ABC"),
        ])


@pytest.fixture
def ctx(db):
    calls = []
    return SimpleNamespace(db=db, calls=calls, shopify=FakeShopify(calls), meli=FakeMeli(calls))


def order_job(db, lines, order_id=88, **extra):
    job_id = db.enqueue_job("shopify_order", f"shopify-order:{order_id}",
                            {"id": order_id, "line_items": lines, **extra}, f"order:{order_id}")
    return db.get_job(job_id)


def reconcile_job(db, order_id=88, sku="ABC", **extra):
    job_id = db.enqueue_job("reconcile_sku", f"shopify-order:{order_id}:{sku}",
                            {"sku": sku, "shopify_order_id": f"gid://shopify/Order/{order_id}", **extra}, f"sku:{sku}")
    return db.get_job(job_id)


def jobs(db):
    with db._connect() as connection:
        return [db.get_job(row[0]) for row in connection.execute("SELECT id FROM jobs WHERE job_type = 'reconcile_sku' ORDER BY id")]


def run(ctx, job=None, **kwargs):
    return handle_reconcile_sku(job or reconcile_job(ctx.db), ctx.db, ctx.shopify, ctx.meli, **kwargs)


def test_shopify_order_enqueues_distinct_exact_skus_without_changing_inventory(ctx):
    job = order_job(ctx.db, [{"sku": "ABC"}, {"sku": "ABC"}, {"sku": " ABC "}])
    handle_shopify_order(job, ctx.db, ctx.shopify)
    handle_shopify_order(job, ctx.db, ctx.shopify)
    assert {(j.source_key, j.resource_key) for j in jobs(ctx.db)} == {
        ("shopify-order:88:ABC", "sku:ABC"), ("shopify-order:88: ABC ", "sku: ABC "),
    }
    assert [j.payload for j in jobs(ctx.db)] == [
        {"sku": " ABC ", "shopify_order_id": "gid://shopify/Order/88"},
        {"sku": "ABC", "shopify_order_id": "gid://shopify/Order/88"},
    ]
    assert ctx.shopify.quantity == 8
    assert ctx.shopify.inventory_adjustments == []
    assert ctx.calls == []


def test_import_enqueue_and_its_shopify_webhook_deduplicate(ctx):
    import_id = ctx.db.enqueue_job("import_meli_order", "meli-order:2001", {"order_id": "2001"}, "order:2001")
    handle_import_meli_order(ctx.db.get_job(import_id), ctx.db, ctx.shopify, ctx.meli)
    handle_shopify_order(order_job(ctx.db, [{"sku": "ABC"}], 77, tags="mercadolibre"), ctx.db, ctx.shopify)
    assert len(jobs(ctx.db)) == 1
    assert jobs(ctx.db)[0].source_key == "shopify-order:77:ABC"
    assert jobs(ctx.db)[0].payload == {"sku": "ABC", "shopify_order_id": "gid://shopify/Order/77"}
    assert ctx.shopify.quantity == 6


@pytest.mark.parametrize("sku", [None, "", "  ", 123])
def test_missing_sku_marks_real_order_and_still_enqueues_valid_lines(ctx, sku):
    job = order_job(ctx.db, [{"sku": sku}, {}, {"sku": "ABC"}])
    for _ in range(2):
        result = handle_shopify_order(job, ctx.db, ctx.shopify)
    assert result["status"] == "needs_review"
    assert ctx.db.get_job(job.id).status == "needs_review"
    note = ctx.shopify.order_reviews[("gid://shopify/Order/88", "shopify-order:88")]
    assert "Line 1" in note and "Line 2" in note and "SKU" in note
    assert len(ctx.shopify.order_reviews) == 1
    assert ctx.shopify.draft_reviews == {}
    assert [j.source_key for j in jobs(ctx.db)] == ["shopify-order:88:ABC"]
    assert ctx.shopify.quantity == 8


@pytest.mark.parametrize("lines", [None, [], {}])
def test_missing_or_invalid_order_lines_require_review(ctx, lines):
    job = order_job(ctx.db, lines)
    assert handle_shopify_order(job, ctx.db, ctx.shopify)["status"] == "needs_review"
    assert ctx.shopify.order_reviews
    assert jobs(ctx.db) == []


def test_order_review_failure_propagates_without_premature_needs_review(ctx):
    job = order_job(ctx.db, [{}])
    ctx.shopify.review_error = RetryableSyncError("unavailable")
    with pytest.raises(RetryableSyncError):
        handle_shopify_order(job, ctx.db, ctx.shopify)
    assert ctx.db.get_job(job.id) == job


def test_two_orders_read_fresh_stock_sequentially(ctx):
    run(ctx, reconcile_job(ctx.db, 1))
    ctx.shopify.quantity = 7
    run(ctx, reconcile_job(ctx.db, 2))
    assert [(listing.sku, quantity) for listing, quantity in ctx.meli.quantity_writes] == [("ABC", 8), ("ABC", 7)]


@pytest.mark.parametrize("import_first", [True, False])
def test_meli_and_shopify_sales_in_either_order_use_final_shopify_quantity(ctx, import_first):
    ctx.shopify.quantity = 10
    import_id = ctx.db.enqueue_job("import_meli_order", "meli-order:2001", {"order_id": "2001"}, "order:2001")

    def meli_sale():
        handle_import_meli_order(ctx.db.get_job(import_id), ctx.db, ctx.shopify, ctx.meli)
        run(ctx, jobs(ctx.db)[-1])

    def shopify_sale():
        ctx.shopify.quantity -= 1  # Shopify already decremented before its webhook.
        handle_shopify_order(order_job(ctx.db, [{"sku": "ABC", "quantity": 1}]), ctx.db, ctx.shopify)
        run(ctx, jobs(ctx.db)[-1])

    for sale in ([meli_sale, shopify_sale] if import_first else [shopify_sale, meli_sale]):
        sale()
    assert ctx.shopify.quantity == 7
    assert ctx.meli.quantity_writes[-1][1] == 7
    assert ctx.shopify.inventory_adjustments == []


def test_shopify_read_follows_listing_resolution_and_ignores_stored_quantity(ctx):
    ctx.meli.on_list = lambda: setattr(ctx.shopify, "quantity", 3)
    result = run(ctx, reconcile_job(ctx.db, quantity=99))
    assert ctx.calls == [("meli-list",), ("shopify-read", "ABC"), ("meli-write", "ABC", 3)]
    assert result["status"] == "updated"
    assert ctx.meli.listings[0].available_quantity == 3


def test_equal_quantity_does_not_write(ctx):
    ctx.shopify.quantity = 10
    assert run(ctx)["status"] == "unchanged"
    assert ctx.meli.quantity_writes == []


@pytest.mark.parametrize("meli_quantity, expected_status", [(0, "unchanged"), (2, "updated")])
def test_negative_shopify_stock_compares_against_sellable_zero(ctx, meli_quantity, expected_status):
    ctx.shopify.quantity = -2
    ctx.meli.listings = [replace(ctx.meli.listings[0], available_quantity=meli_quantity)]
    assert run(ctx)["status"] == expected_status
    assert ctx.meli.listings[0].available_quantity == 0


def test_exact_variation_is_updated_without_other_sku_matches(ctx):
    target = MeliListing("MLC1", "12", "ABC", 4)
    other = MeliListing("MLC1", "13", " ABC ", 5)
    ctx.meli.listings = [other, target]
    run(ctx)
    assert ctx.meli.quantity_writes == [(target, 8)]
    assert ctx.meli.listings[0] == other


@pytest.mark.parametrize("matches", [[], [MeliListing("MLC1", None, "ABC", 1), MeliListing("MLC2", "12", "ABC", 2)]])
def test_missing_or_duplicate_meli_match_marks_real_order_without_stock_write(ctx, matches):
    ctx.meli.listings = matches
    job = reconcile_job(ctx.db)
    assert run(ctx, job)["status"] == "needs_review"
    assert ctx.db.get_job(job.id).status == "needs_review"
    assert ctx.shopify.order_reviews[("gid://shopify/Order/88", "sku:ABC")]
    assert ctx.shopify.draft_reviews == {}
    assert ctx.meli.quantity_writes == []


@pytest.mark.parametrize("failure", ["list", "quantity", "write"])
def test_temporary_api_errors_propagate_for_worker_retry(ctx, failure):
    job = reconcile_job(ctx.db)
    error = RetryableSyncError("rate limited")
    setattr(ctx.meli if failure != "quantity" else ctx.shopify, f"{failure}_error", error)
    with pytest.raises(RetryableSyncError):
        run(ctx, job)
    assert ctx.db.get_job(job.id) == job
    assert ctx.shopify.order_reviews == {}
    assert ctx.meli.quantity_writes == []


@pytest.mark.parametrize("failure", ["list", "quantity", "write"])
def test_permanent_api_errors_mark_real_order(ctx, failure):
    error = ReviewRequiredError("sku:ABC", "unsafe mapping", {})
    setattr(ctx.meli if failure != "quantity" else ctx.shopify, f"{failure}_error", error)
    assert run(ctx)["status"] == "needs_review"
    assert "unsafe mapping" in ctx.shopify.order_reviews[("gid://shopify/Order/88", "sku:ABC")]
    assert ctx.meli.quantity_writes == []


@pytest.mark.parametrize("error", [RetryableSyncError("offline"), ReviewRequiredError("sku:ABC", "cannot mark order", {})])
def test_reconcile_review_failure_leaves_job_retryable(ctx, error):
    job = reconcile_job(ctx.db)
    ctx.meli.listings = []
    ctx.shopify.review_error = error
    with pytest.raises(type(error)):
        run(ctx, job)
    assert ctx.db.get_job(job.id) == job
    assert ctx.db.count("review_links") == 0


@pytest.mark.parametrize("unsafe", [False, True])
def test_dry_run_reads_and_compares_without_business_mutations(ctx, unsafe):
    job = reconcile_job(ctx.db)
    if unsafe:
        ctx.shopify.quantity_error = ReviewRequiredError("sku:ABC", "duplicate Shopify SKU", {})
    with ctx.db._connect() as connection:
        before = list(connection.iterdump())
    result = run(ctx, job, dry_run=True)
    assert result["status"] == ("needs_review" if unsafe else "dry_run")
    if not unsafe:
        assert result["shopify_quantity"] == 8
        assert result["meli_quantity"] == 10
        assert result["target_quantity"] == 8
    assert ctx.calls == [("meli-list",), ("shopify-read", "ABC")]
    assert ctx.meli.quantity_writes == []
    assert ctx.shopify.order_reviews == {}
    assert ctx.shopify.draft_reviews == {}
    with ctx.db._connect() as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("sku", ["", "  ", None])
def test_blank_reconciliation_sku_cannot_match_blank_marketplace_listings(ctx, sku):
    ctx.meli.listings = [MeliListing("MLC1", None, sku, 10)]
    assert run(ctx, reconcile_job(ctx.db, sku=sku))["status"] == "needs_review"
    assert ctx.meli.quantity_writes == []
    assert ctx.shopify.order_reviews


@pytest.mark.parametrize("match_count", [0, 2])
def test_real_shopify_client_unsafe_matches_mark_real_order(ctx, match_count):
    from stock_sync.shopify import ShopifyClient
    from test_shopify import FakeTransport, variant_page

    transport = FakeTransport()
    page = variant_page("ABC", 11)
    page["productVariants"]["nodes"] = [
        variant_page("ABC", index)["productVariants"]["nodes"][0] for index in range(match_count)
    ]
    transport.pages = [page]
    ctx.shopify = ShopifyClient(None, transport=transport)
    assert run(ctx, reconcile_job(ctx.db, 77))["status"] == "needs_review"
    assert ctx.meli.quantity_writes == []
    operation, variables = transport.calls[-1]
    assert operation == "mark_order_review"
    assert variables["input"]["id"] == "gid://shopify/Order/77"
    assert "meli-needs-review" in variables["input"]["tags"]
    assert "ABC" in variables["input"]["note"]


def test_real_shopify_order_review_preserves_existing_order_metadata(ctx):
    from stock_sync.shopify import ShopifyClient
    from test_shopify import FakeTransport

    transport = FakeTransport()
    shopify = ShopifyClient(None, transport=transport)
    job = order_job(ctx.db, [{}], 77)
    assert handle_shopify_order(job, ctx.db, shopify)["status"] == "needs_review"
    assert [operation for operation, _ in transport.calls] == ["find_order", "mark_order_review"]
    order_input = transport.calls[-1][1]["input"]
    assert order_input["id"] == "gid://shopify/Order/77"
    assert "existing-tag" in order_input["tags"]
    assert "meli-needs-review" in order_input["tags"]
    assert "Existing order note" in order_input["note"]
    assert "Line 1: missing SKU" in order_input["note"]
