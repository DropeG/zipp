from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.handlers import handle_import_meli_order
from stock_sync.models import MeliOrder, MeliOrderLine, ShopifyVariant


def line(sku="ABC", quantity=2, price="12990", currency="CLP"):
    return MeliOrderLine("MLC1", None, "Widget", quantity, price, currency, sku)


def variant(sku="ABC", variant_id=11, tracked=True):
    return ShopifyVariant(f"gid://shopify/ProductVariant/{variant_id}", sku, 7, tracked)


class FakeMeli:
    def __init__(self):
        self.order = MeliOrder("2001", "100", "paid", "2026-09-06T12:00:00+00:00", [line()])
        self.error = None
        self.requested_ids = []

    def get_order(self, order_id):
        self.requested_ids.append(order_id)
        if self.error:
            raise self.error
        return self.order


class FakeShopify:
    def __init__(self):
        self.matches = {"ABC": [variant()], "XYZ": [variant("XYZ", 22)]}
        self.existing_order_id = None
        self.created_orders = []
        self.review_calls = []
        self.lookups = []
        self.variant_errors = {}
        self.create_error = None
        self.review_error = None

    def find_variants_by_skus(self, skus):
        result = {}
        for sku in skus:
            if sku in self.variant_errors:
                raise self.variant_errors[sku]
            result[sku] = self.matches.get(sku, [])
        return result

    def find_imported_order(self, order_id):
        self.lookups.append(order_id)
        return self.existing_order_id

    def create_imported_order(self, order, variants):
        assert self.lookups == ["2001"]
        if self.create_error:
            raise self.create_error
        self.created_orders.append((order, variants))
        self.existing_order_id = "gid://shopify/Order/77"
        return self.existing_order_id

    def create_or_update_review(self, review_key, note):
        self.review_calls.append((review_key, note))
        if self.review_error:
            raise self.review_error
        return "gid://shopify/DraftOrder/91"


@pytest.fixture
def ctx(db):
    job_id = db.enqueue_job("import_meli_order", "meli-order:2001", {"order_id": "2001"}, "order:2001")
    return SimpleNamespace(db=db, job=db.get_job(job_id), meli=FakeMeli(), shopify=FakeShopify())


def run(ctx, **kwargs):
    return handle_import_meli_order(ctx.job, ctx.db, ctx.shopify, ctx.meli, **kwargs)


def reconcile_jobs(db):
    with db._connect() as connection:
        return [db.get_job(row[0]) for row in connection.execute(
            "SELECT id FROM jobs WHERE job_type = 'reconcile_sku' ORDER BY id"
        )]


def assert_no_business_mutations(ctx):
    assert ctx.shopify.created_orders == []
    assert ctx.shopify.review_calls == []
    assert ctx.db.count("order_links") == 0
    assert ctx.db.count("review_links") == 0
    assert ctx.db.count("jobs") == 1
    assert ctx.db.get_job(ctx.job.id) == ctx.job


def test_paid_order_creates_one_complete_order_and_distinct_reconciliation_jobs(ctx):
    ctx.meli.order = replace(ctx.meli.order, lines=[line(), line("XYZ", 1), line("ABC", 1, "9990")])
    result = run(ctx)
    assert ctx.meli.requested_ids == ["2001"]
    assert len(ctx.shopify.created_orders) == 1
    order, variants = ctx.shopify.created_orders[0]
    assert order.lines == [line(), line("XYZ", 1), line("ABC", 1, "9990")]
    assert variants == {"ABC": variant(), "XYZ": variant("XYZ", 22)}
    assert result["shopify_order_id"] == "gid://shopify/Order/77"
    assert ctx.db.get_order_link("2001") == "gid://shopify/Order/77"
    assert {(job.source_key, job.resource_key, job.payload["sku"]) for job in reconcile_jobs(ctx.db)} == {
        ("shopify-order:77:ABC", "sku:ABC", "ABC"),
        ("shopify-order:77:XYZ", "sku:XYZ", "XYZ"),
    }


@pytest.mark.parametrize("status", ["confirmed", "payment_required", "cancelled"])
def test_non_paid_order_has_no_mutations_even_with_invalid_lines(ctx, status):
    ctx.meli.order = replace(ctx.meli.order, status=status, lines=[])
    assert run(ctx)["status"] == "skipped"
    assert_no_business_mutations(ctx)


def test_collects_all_line_problems_before_creating_one_review(ctx):
    ctx.meli.order = replace(ctx.meli.order, lines=[line("", 0), line("MISSING"), line("XYZ"), line("ABC")])
    ctx.shopify.matches["XYZ"] = [variant("XYZ", 22), variant("XYZ", 33)]
    ctx.shopify.matches["ABC"] = [variant(tracked=False)]
    result = run(ctx)
    assert result["status"] == "needs_review"
    assert ctx.shopify.created_orders == []
    assert ctx.db.get_order_link("2001") is None
    assert reconcile_jobs(ctx.db) == []
    assert len(ctx.shopify.review_calls) == 1
    key, note = ctx.shopify.review_calls[0]
    assert key == "order:2001"
    for problem in ["SKU", "quantity", "MISSING", "XYZ", "ABC", "tracked"]:
        assert problem in note
    assert ctx.db.get_review_link(key) == "gid://shopify/DraftOrder/91"
    assert ctx.db.get_job(ctx.job.id).status == "needs_review"


@pytest.mark.parametrize("bad_lines", [[], [line(" ")], [line(quantity=-1)], [line(quantity=True)],
                                        [line(quantity=1.5)], [line(price="NaN")],
                                        [line(price="-1")], [line(currency="")],
                                        [line(), line("XYZ", currency="USD")]])
def test_unsafe_order_never_creates_partial_order(ctx, bad_lines):
    ctx.meli.order = replace(ctx.meli.order, lines=bad_lines)
    assert run(ctx)["status"] == "needs_review"
    assert ctx.shopify.created_orders == []
    assert len(ctx.shopify.review_calls) == 1
    assert reconcile_jobs(ctx.db) == []


def test_non_exact_variant_is_rejected(ctx):
    ctx.shopify.matches["ABC"] = [variant("abc")]
    assert run(ctx)["status"] == "needs_review"
    assert ctx.shopify.created_orders == []


def test_retry_uses_local_link_and_repairs_missing_reconcile_jobs(ctx):
    ctx.db.link_order("2001", "gid://shopify/Order/77")
    ctx.db.enqueue_job("reconcile_sku", "shopify-order:77:ABC", {"sku": "ABC"}, "sku:ABC")
    ctx.meli.order = replace(ctx.meli.order, lines=[line(), line("XYZ")])
    run(ctx)
    run(ctx)
    assert ctx.shopify.lookups == []
    assert ctx.shopify.created_orders == []
    assert {job.source_key for job in reconcile_jobs(ctx.db)} == {"shopify-order:77:ABC", "shopify-order:77:XYZ"}


def test_crash_recovery_uses_shopify_lookup_and_saves_link(ctx):
    ctx.shopify.existing_order_id = "gid://shopify/Order/77"
    run(ctx)
    assert ctx.db.get_order_link("2001") == "gid://shopify/Order/77"
    assert ctx.shopify.created_orders == []
    assert len(reconcile_jobs(ctx.db)) == 1


def test_crash_after_external_create_does_not_create_second_order(ctx, monkeypatch):
    original_link = ctx.db.link_order

    def failed_link(*args):
        raise OSError("disk unavailable after Shopify creation")

    monkeypatch.setattr(ctx.db, "link_order", failed_link)
    with pytest.raises(OSError):
        run(ctx)
    assert len(ctx.shopify.created_orders) == 1
    assert ctx.db.get_order_link("2001") is None

    monkeypatch.setattr(ctx.db, "link_order", original_link)
    run(ctx)
    assert len(ctx.shopify.created_orders) == 1
    assert ctx.db.get_order_link("2001") == "gid://shopify/Order/77"
    assert len(reconcile_jobs(ctx.db)) == 1


def test_dry_run_returns_resolved_order_and_does_not_mutate(ctx):
    result = run(ctx, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["lines"] == [{"sku": "ABC", "variant_id": "gid://shopify/ProductVariant/11",
                                 "quantity": 2, "unit_price": "12990", "currency": "CLP"}]
    assert result["tags"] == ["mercadolibre", "meli-order-2001"]
    assert result["inventory_behaviour"] == "DECREMENT_IGNORING_POLICY"
    assert_no_business_mutations(ctx)


def test_dry_run_validation_problem_does_not_create_review_or_change_job(ctx):
    ctx.meli.order = replace(ctx.meli.order, lines=[line("")])
    result = run(ctx, dry_run=True)
    assert result["status"] == "needs_review"
    assert result["problems"]
    assert_no_business_mutations(ctx)


@pytest.mark.parametrize("existing_link", [False, True])
def test_dry_run_existing_order_does_not_save_link_or_enqueue(ctx, existing_link):
    ctx.shopify.existing_order_id = "gid://shopify/Order/77"
    if existing_link:
        ctx.db.link_order("2001", "gid://shopify/Order/77")
    with ctx.db._connect() as connection:
        before = list(connection.iterdump())
    result = run(ctx, dry_run=True)
    assert result["shopify_order_id"] == "gid://shopify/Order/77"
    with ctx.db._connect() as connection:
        assert list(connection.iterdump()) == before
    assert ctx.shopify.created_orders == []
    assert ctx.shopify.review_calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_meli_permanent_validation_error_becomes_order_review(ctx, dry_run):
    ctx.meli.error = ReviewRequiredError("order:2001", "invalid unit price", {})
    assert run(ctx, dry_run=dry_run)["status"] == "needs_review"
    if dry_run:
        assert_no_business_mutations(ctx)
    else:
        assert ctx.shopify.review_calls == [("order:2001", "invalid unit price")]
        assert ctx.db.get_job(ctx.job.id).status == "needs_review"


def test_variant_permanent_errors_are_collected_across_skus(ctx):
    ctx.meli.order = replace(ctx.meli.order, lines=[line(), line("XYZ")])
    for sku in ["ABC", "XYZ"]:
        ctx.shopify.variant_errors[sku] = ReviewRequiredError(f"sku:{sku}", f"{sku}: unavailable inventory", {})
    run(ctx)
    assert all(sku in ctx.shopify.review_calls[0][1] for sku in ["ABC", "XYZ"])
    assert ctx.shopify.created_orders == []


@pytest.mark.parametrize("operation", ["meli", "variants", "create"])
def test_transient_failures_propagate_for_worker_retry(ctx, operation):
    error = RetryableSyncError("temporarily unavailable")
    if operation == "meli":
        ctx.meli.error = error
    elif operation == "variants":
        ctx.shopify.variant_errors["ABC"] = error
    else:
        ctx.shopify.create_error = error
    with pytest.raises(RetryableSyncError):
        run(ctx)
    assert_no_business_mutations(ctx)


def test_create_validation_error_creates_review_without_link(ctx):
    ctx.shopify.create_error = ReviewRequiredError("order:2001", "invalid Shopify lineItems", {})
    assert run(ctx)["status"] == "needs_review"
    assert ctx.db.count("order_links") == 0
    assert ctx.db.get_job(ctx.job.id).status == "needs_review"


def test_review_failure_propagates_without_repeated_mutation_attempt(ctx):
    ctx.meli.order = replace(ctx.meli.order, lines=[])
    ctx.shopify.review_error = ReviewRequiredError("order:2001", "draft rejected", {})
    with pytest.raises(ReviewRequiredError, match="draft rejected"):
        run(ctx)
    assert len(ctx.shopify.review_calls) == 1
    assert ctx.db.count("review_links") == 0
    assert ctx.db.get_job(ctx.job.id) == ctx.job
