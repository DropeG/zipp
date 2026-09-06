from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from stock_sync.daily import run_daily
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.models import MeliListing, MeliOrder, MeliOrderLine, ShopifyVariant


def variant(sku="ABC", quantity=6, id="1", tracked=True):
    return ShopifyVariant(f"gid://shopify/ProductVariant/{id}", sku, quantity, tracked)


def listing(sku="ABC", quantity=9, id="MLC1", variation_id=None):
    return MeliListing(id, variation_id, sku, quantity)


class Shopify:
    def __init__(self, calls):
        self.calls = calls
        self.catalog = [variant()]
        self.orders = {}
        self.reviews = {}
        self.resolved = []
        self.fresh_quantity = None
        self.quantity_error = None
        self.review_error = None

    def find_variants_by_skus(self, skus):
        return {sku: [v for v in self.catalog if v.sku == sku] for sku in skus}

    def list_all_variants(self):
        self.calls.append("catalog:shopify")
        return list(self.catalog)

    def get_available_quantity(self, sku):
        self.calls.append(f"quantity:{sku}")
        if self.quantity_error:
            raise self.quantity_error
        return self.fresh_quantity if self.fresh_quantity is not None else self.find_variants_by_skus([sku])[sku][0].available_quantity

    def find_imported_order(self, order_id):
        return self.orders.get(order_id)

    def create_imported_order(self, order, variants):
        self.calls.append(f"import:{order.order_id}")
        self.orders[order.order_id] = f"gid://shopify/Order/{order.order_id}"
        for line in order.lines:
            self.catalog = [replace(v, available_quantity=v.available_quantity-line.quantity) if v.sku == line.sku else v for v in self.catalog]
        return self.orders[order.order_id]

    def create_or_update_review(self, review_key, note, *, draft_id=None):
        if self.review_error:
            raise self.review_error
        self.reviews[review_key] = note
        return f"draft:{review_key}"

    def resolve_review(self, review_key, *, draft_id=None, shopify_order_id=None):
        self.resolved.append(review_key)

    def get_order_skus(self, order_id):
        return ["ABC"]


class Meli:
    def __init__(self, calls):
        self.calls = calls
        self.paid_orders = []
        self.catalog = [listing()]
        self.quantity_writes = []
        self.since = []
        self.error = None
        self.write_error = None

    def search_paid_orders(self, since):
        self.since.append(since)
        return self.paid_orders

    def get_order(self, order_id):
        if self.error:
            raise self.error
        return MeliOrder(order_id, "100", "paid", "2026-09-06T00:00:00+00:00", [
            MeliOrderLine("MLC1", None, "Widget", 2, "1000", "CLP", "ABC")])

    def list_all_listings(self):
        self.calls.append("catalog:meli")
        return list(self.catalog)

    def set_available_quantity(self, item, quantity):
        if self.write_error:
            raise self.write_error
        self.quantity_writes.append((item, quantity))
        self.catalog = [replace(v, available_quantity=quantity) if v == item else v for v in self.catalog]


@pytest.fixture
def ctx(db, clock):
    calls = []
    return SimpleNamespace(db=db, now=clock.now(), calls=calls, shopify=Shopify(calls), meli=Meli(calls))


def run(ctx, **kwargs):
    return run_daily(ctx.db, ctx.shopify, ctx.meli, ctx.now, **kwargs)


def test_recovers_every_order_before_either_catalog_and_retries_idempotently(ctx):
    ctx.meli.paid_orders = ["2001", "2002"]
    result = run(ctx)
    assert ctx.calls[:4] == ["import:2001", "import:2002", "catalog:shopify", "catalog:meli"]
    assert ctx.shopify.catalog[0].available_quantity == 2
    assert ctx.meli.quantity_writes[-1][1] == 2
    assert result.updated == 1
    run(ctx)
    assert ctx.calls.count("import:2001") == ctx.calls.count("import:2002") == 1
    assert ctx.db.count("order_links") == 2
    with ctx.db._connect() as connection:
        assert [r[0] for r in connection.execute("SELECT status FROM jobs WHERE job_type = 'import_meli_order'")] == ["completed", "completed"]


def test_first_recovery_uses_30_days_and_later_checkpoint_one_day_overlap(ctx):
    run(ctx)
    assert ctx.meli.since == [ctx.now - timedelta(days=30)]
    assert ctx.db.get_checkpoint("daily_orders_completed_at") == ctx.now.isoformat()
    ctx.now += timedelta(days=3)
    run(ctx)
    assert ctx.meli.since[-1] == ctx.now - timedelta(days=4)


@pytest.mark.parametrize("error", [RetryableSyncError("offline"), ReviewRequiredError("order:2001", "bad order", {})])
def test_failed_recovery_stops_before_catalogs_and_keeps_previous_checkpoint(ctx, error):
    checkpoint = (ctx.now - timedelta(days=2)).isoformat()
    ctx.db.set_checkpoint("daily_orders_completed_at", checkpoint)
    ctx.meli.paid_orders = ["2001"]
    ctx.meli.error = error
    if isinstance(error, RetryableSyncError):
        with pytest.raises(RetryableSyncError):
            run(ctx)
    else:
        result = run(ctx)
        assert result.status == "needs_review"
        assert ctx.shopify.reviews["order:2001"]
    assert ctx.calls == []
    assert ctx.db.get_checkpoint("daily_orders_completed_at") == checkpoint
    assert ctx.meli.quantity_writes == []


def test_daily_uses_fresh_shopify_quantity_and_never_adjusts_it(ctx):
    ctx.shopify.fresh_quantity = 3
    result = run(ctx)
    assert ctx.meli.quantity_writes == [(listing(), 3)]
    assert ctx.shopify.catalog == [variant()]
    assert ctx.shopify.orders == {}
    assert result.updated == 1
    assert ctx.calls[-1] == "quantity:ABC"


@pytest.mark.parametrize("quantity, writes", [(9, []), (-2, [(listing(), 0)])])
def test_equal_quantity_no_write_and_negative_quantity_clamps_to_zero(ctx, quantity, writes):
    ctx.shopify.catalog = [variant(quantity=quantity)]
    run(ctx)
    assert ctx.meli.quantity_writes == writes


@pytest.mark.parametrize("shopify, meli", [
    ([variant(), variant(id="2")], [listing()]),
    ([variant()], [listing(), listing(id="MLC2")]),
    ([], [listing()]), ([variant()], []),
    ([variant(tracked=False)], [listing()]),
])
def test_unsafe_mapping_updates_one_stable_review_and_resolves_after_repair(ctx, shopify, meli):
    ctx.shopify.catalog, ctx.meli.catalog = shopify, meli
    run(ctx)
    run(ctx)
    assert ctx.meli.quantity_writes == []
    assert len(ctx.shopify.reviews) == ctx.db.count("review_links") == 1
    key = next(iter(ctx.shopify.reviews))
    assert key.startswith("product:")
    assert "ABC" in ctx.shopify.reviews[key]
    ctx.shopify.catalog, ctx.meli.catalog = [variant()], [listing()]
    run(ctx)
    assert key in ctx.shopify.resolved
    assert ctx.meli.quantity_writes == [(listing(), 6)]


def test_blank_skus_use_separate_stable_entity_reviews_then_resolve_when_corrected(ctx):
    ctx.shopify.catalog = [variant(""), variant(" ", id="2")]
    ctx.meli.catalog = [listing(""), listing(" ", id="MLC2", variation_id="9")]
    run(ctx)
    run(ctx)
    assert ctx.meli.quantity_writes == []
    assert len(ctx.shopify.reviews) == 4
    blank_keys = set(ctx.shopify.reviews)
    ctx.shopify.catalog = [variant("ABC"), variant("DEF", id="2")]
    ctx.meli.catalog = [listing("ABC"), listing("DEF", id="MLC2", variation_id="9")]
    run(ctx)
    assert blank_keys <= set(ctx.shopify.resolved)


def test_skus_match_exactly_and_variation_writes_keep_identity(ctx):
    ctx.shopify.catalog = [variant("ABC"), variant(" ABC ", 4, id="2")]
    ctx.meli.catalog = [listing("ABC"), listing(" ABC ", 7, variation_id="9")]
    run(ctx)
    assert ctx.meli.quantity_writes == [(listing(" ABC ", 7, variation_id="9"), 4), (listing(), 6)]


@pytest.mark.parametrize("stage", ["quantity", "write"])
def test_permanent_product_failure_creates_review_and_does_not_resolve(ctx, stage):
    error = ReviewRequiredError("sku:ABC", "unsafe quantity", {})
    setattr(ctx.shopify if stage == "quantity" else ctx.meli, f"{stage}_error", error)
    run(ctx)
    assert len(ctx.shopify.reviews) == 1
    assert "unsafe quantity" in next(iter(ctx.shopify.reviews.values()))
    assert ctx.shopify.resolved == []


def test_temporary_product_failure_propagates_after_recovery_checkpoint(ctx):
    ctx.meli.write_error = RetryableSyncError("offline")
    with pytest.raises(RetryableSyncError):
        run(ctx)
    assert ctx.db.get_checkpoint("daily_orders_completed_at") == ctx.now.isoformat()
    assert ctx.shopify.reviews == {}


@pytest.mark.parametrize("unsafe", [False, True])
def test_dry_run_has_no_database_or_business_writes(ctx, unsafe):
    ctx.meli.paid_orders = ["2001"]
    if unsafe:
        ctx.shopify.catalog.append(variant(id="2"))
    with ctx.db._connect() as connection:
        before = list(connection.iterdump())
    result = run(ctx, dry_run=True)
    assert result.status == ("needs_review" if unsafe else "dry_run")
    assert ctx.shopify.orders == ctx.shopify.reviews == {}
    assert ctx.shopify.resolved == ctx.meli.quantity_writes == []
    if not unsafe:
        assert result.updated == 0
        assert result.planned_updates == [{"sku": "ABC", "item_id": "MLC1", "variation_id": None, "quantity": 6}]
    with ctx.db._connect() as connection:
        assert list(connection.iterdump()) == before


def test_rejects_naive_now_before_any_calls(ctx):
    with pytest.raises(ValueError, match="timezone"):
        run_daily(ctx.db, ctx.shopify, ctx.meli, ctx.now.replace(tzinfo=None))
    assert ctx.meli.since == ctx.calls == []


def test_partial_recovery_failure_retries_completed_import_without_duplication(ctx, monkeypatch):
    ctx.meli.paid_orders = ["2001", "2002"]
    get_order = ctx.meli.get_order

    def fail_second(order_id):
        if order_id == "2002":
            raise RetryableSyncError("second order unavailable")
        return get_order(order_id)

    monkeypatch.setattr(ctx.meli, "get_order", fail_second)
    with pytest.raises(RetryableSyncError):
        run(ctx)
    assert ctx.db.get_checkpoint("daily_orders_completed_at") is None
    assert ctx.calls == ["import:2001"]
    monkeypatch.setattr(ctx.meli, "get_order", get_order)
    run(ctx)
    assert ctx.calls.count("import:2001") == ctx.calls.count("import:2002") == 1
    assert ctx.meli.quantity_writes == [(listing(), 2)]


def test_untracked_shopify_only_products_are_outside_managed_catalog(ctx):
    ctx.shopify.catalog = [variant(), variant("SERVICE", id="2", tracked=False), variant("", id="3", tracked=False)]
    run(ctx)
    assert ctx.shopify.reviews == {}
    assert ctx.meli.quantity_writes == [(listing(), 6)]


def test_clean_match_resolves_remote_review_even_after_lost_local_link(ctx):
    ctx.meli.catalog = []
    run(ctx)
    key = next(iter(ctx.shopify.reviews))
    with ctx.db._connect() as connection:
        connection.execute("DELETE FROM review_links")
    ctx.meli.catalog = [listing()]
    run(ctx)
    assert key in ctx.shopify.resolved


def test_product_reviews_use_distinct_search_safe_tags_for_exact_skus_and_blank_entities(ctx):
    from stock_sync.shopify import ShopifyClient
    from test_shopify import FakeTransport

    ctx.shopify.catalog = [variant(' A:B ("C") ', id="1"), variant('A:B ("C")', id="2"), variant("", id="3")]
    ctx.meli.catalog = [listing("", variation_id="9")]
    run(ctx)
    assert len(ctx.shopify.reviews) == 4
    transport = FakeTransport()
    shopify = ShopifyClient(None, transport=transport)
    for key, note in ctx.shopify.reviews.items():
        shopify.create_or_update_review(key, note)
    searches = [variables["query"] for operation, variables in transport.calls if operation == "find_review"]
    assert len(set(searches)) == 4
    for query in searches:
        tag_value = query.removeprefix("tag:")
        assert all(character.isalnum() or character == "-" for character in tag_value)
        assert len(tag_value) <= 255


def test_real_clients_consume_all_order_and_catalog_pages_before_copying(ctx):
    from stock_sync.meli import MeliClient
    from stock_sync.shopify import ShopifyClient
    from test_meli import item, order, page
    from test_shopify import default_reply, variant_page

    class GraphQL:
        def __init__(self):
            self.order_count = 0
            self.cursors = []

        def execute(self, operation, query, variables):
            ctx.calls.append(operation)
            if operation == "find_variants":
                sku = "XYZ" if "XYZ" in variables["query"] else "ABC"
                return variant_page(sku, 2 if sku == "XYZ" else 1)
            if operation == "create_imported_order":
                self.order_count += 1
                return {"orderCreate": {"order": {"id": f"gid://shopify/Order/{self.order_count}"}, "userErrors": []}}
            if operation == "list_all_variants":
                self.cursors.append(variables["after"])
                return variant_page("ABC", 1, True) if variables["after"] is None else variant_page("XYZ", 2)
            return default_reply(operation)

    class REST:
        def __init__(self):
            self.order_offsets = []
            self.scroll_ids = []
            self.writes = []

        def request(self, method, path, **kwargs):
            ctx.calls.append(path)
            if path == "/orders/search":
                offset = kwargs["params"]["offset"]
                self.order_offsets.append(offset)
                return page([{"id": 2001 + offset}], 2, offset)
            if path.startswith("/orders/"):
                id = int(path.rsplit("/", 1)[-1])
                return order(order_id=id, sku="ABC" if id == 2001 else "XYZ")
            if path == "/users/100/items/search":
                scroll_id = kwargs["params"].get("scroll_id")
                self.scroll_ids.append(scroll_id)
                return page(["MLC1"], 2, scroll_id="next") if scroll_id is None else page(["MLC2"], 2)
            if path in {"/items/MLC1", "/items/MLC2"}:
                id = path.rsplit("/", 1)[-1]
                result = item(id, sku="ABC" if id == "MLC1" else "XYZ")
                result["available_quantity"] = 9
                if method == "PUT":
                    self.writes.append((id, kwargs["json"]))
                    result.update(kwargs["json"])
                return result
            raise AssertionError(f"Unexpected REST request: {method} {path}")

    graphql, rest = GraphQL(), REST()
    shopify = ShopifyClient(None, transport=graphql)
    meli = MeliClient(SimpleNamespace(meli_expected_seller_id="100"), transport=rest)
    result = run_daily(ctx.db, shopify, meli, ctx.now)
    assert result.recovered == result.updated == 2
    assert ctx.db.get_order_link("2001") == "gid://shopify/Order/1"
    assert ctx.db.get_order_link("2002") == "gid://shopify/Order/2"
    assert rest.order_offsets == [0, 1]
    assert graphql.cursors == [None, "cursor-1"]
    assert rest.scroll_ids == [None, "next"]
    assert rest.writes == [("MLC1", {"available_quantity": 7}), ("MLC2", {"available_quantity": 7})]
    imports = [index for index, call in enumerate(ctx.calls) if call == "create_imported_order"]
    assert max(imports) < ctx.calls.index("list_all_variants") < ctx.calls.index("/users/100/items/search")
