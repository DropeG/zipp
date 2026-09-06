from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from stock_sync.config import Settings
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.models import MeliOrder, MeliOrderLine, ShopifyVariant
from stock_sync.shopify import ShopifyClient


@dataclass
class FakeTransport:
    pages: list[dict[str, Any]]
    replies: dict[str, dict[str, Any]]
    last_operation: str | None = None
    last_query: str | None = None
    last_variables: dict[str, Any] | None = None
    calls: list[tuple[str, dict[str, Any]]] | None = None

    def __init__(self) -> None:
        self.pages = []
        self.replies = {}
        self.last_operation = None
        self.last_query = None
        self.last_variables = None
        self.calls = []

    def execute(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.last_operation = operation
        self.last_query = query
        self.last_variables = variables
        self.calls.append((operation, variables))
        if operation == "find_variants":
            return self.pages.pop(0) if self.pages else variant_page("ABC", 11)
        return self.replies.get(operation, default_reply(operation))

    def reply_with_user_error(self, field: str, message: str) -> None:
        self.replies["create_imported_order"] = {
            "orderCreate": {"order": None, "userErrors": [{"field": [field], "message": message}]}
        }


def default_reply(operation: str) -> dict[str, Any]:
    if operation == "find_imported_order":
        return {"orders": {"nodes": []}}
    if operation == "create_imported_order":
        return {"orderCreate": {"order": {"id": "gid://shopify/Order/77"}, "userErrors": []}}
    if operation == "find_review":
        return {"draftOrders": {"nodes": []}}
    if operation == "find_order":
        return {
            "node": {
                "id": "gid://shopify/Order/77",
                "note": "Existing order note",
                "tags": ["existing-tag"],
            }
        }
    if operation == "create_review":
        return {"draftOrderCreate": {"draftOrder": {"id": "gid://shopify/DraftOrder/91"}, "userErrors": []}}
    if operation in {"update_review", "resolve_review"}:
        return {"draftOrderUpdate": {"draftOrder": {"id": "gid://shopify/DraftOrder/91"}, "userErrors": []}}
    if operation == "mark_order_review":
        return {"orderUpdate": {"order": {"id": "gid://shopify/Order/77"}, "userErrors": []}}
    raise AssertionError(f"Unexpected operation: {operation}")


def variant_page(sku: str, variant_id: int, has_next: bool = False) -> dict[str, Any]:
    return {
        "productVariants": {
            "nodes": [
                {
                    "id": f"gid://shopify/ProductVariant/{variant_id}",
                    "sku": sku,
                    "inventoryQuantity": 7,
                    "inventoryItem": {"tracked": True},
                }
            ],
            "pageInfo": {"hasNextPage": has_next, "endCursor": f"cursor-{variant_id}" if has_next else None},
        }
    }


def meli_order() -> MeliOrder:
    return MeliOrder(
        order_id="2001",
        seller_id="100",
        status="paid",
        processed_at="2026-09-06T12:00:00+00:00",
        lines=[
            MeliOrderLine(
                item_id="MLC1",
                variation_id=None,
                title="Widget",
                quantity=2,
                unit_price="12990",
                currency="CLP",
                sku="ABC",
            )
        ],
    )


def variant(sku: str, variant_id: int = 11, available_quantity: int = 7) -> ShopifyVariant:
    return ShopifyVariant(
        variant_id=f"gid://shopify/ProductVariant/{variant_id}",
        sku=sku,
        available_quantity=available_quantity,
        inventory_tracked=True,
    )


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def shopify(transport: FakeTransport) -> ShopifyClient:
    settings = Settings(
        shopify_shop_url="https://example.myshopify.com/",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        meli_app_id="meli-app-id",
        meli_client_secret="meli-secret",
        meli_expected_seller_id="100",
        meli_webhook_token="meli-webhook-token",
        shopify_api_version="2026-07",
        max_webhook_bytes=1024,
        database_path="stock_sync.db",
    )
    return ShopifyClient(settings, transport=transport)


def test_create_imported_order_uses_meli_prices_and_safe_options(shopify, transport):
    order_id = shopify.create_imported_order(meli_order(), {"ABC": variant("ABC")})

    variables = transport.last_variables
    assert order_id == "gid://shopify/Order/77"
    assert variables["order"]["financialStatus"] == "PAID"
    assert variables["order"]["sourceIdentifier"] == "2001"
    assert variables["order"]["processedAt"] == "2026-09-06T12:00:00+00:00"
    assert variables["order"]["tags"] == ["mercadolibre", "meli-order-2001"]
    assert variables["order"]["lineItems"][0]["variantId"].endswith("/11")
    assert variables["order"]["lineItems"][0]["priceSet"]["shopMoney"] == {
        "amount": "12990",
        "currencyCode": "CLP",
    }
    assert variables["options"] == {
        "inventoryBehaviour": "DECREMENT_IGNORING_POLICY",
        "sendReceipt": False,
        "sendFulfillmentReceipt": False,
    }


def test_create_imported_order_checks_existing_tag_before_creating(shopify, transport):
    shopify.create_imported_order(meli_order(), {"ABC": variant("ABC")})

    assert [operation for operation, _ in transport.calls] == [
        "find_imported_order",
        "create_imported_order",
    ]


def test_find_imported_order_returns_tag_match(shopify, transport):
    transport.replies["find_imported_order"] = {
        "orders": {"nodes": [{"id": "gid://shopify/Order/77"}]}
    }

    assert shopify.find_imported_order("2001") == "gid://shopify/Order/77"
    assert transport.last_variables == {"query": "tag:meli-order-2001"}


def test_duplicate_sku_returns_both_variants(shopify, transport):
    transport.pages = [variant_page("ABC", 11, has_next=True), variant_page("ABC", 22)]

    assert shopify.find_variants_by_skus({"ABC"}) == {
        "ABC": [variant("ABC", 11), variant("ABC", 22)]
    }


def test_variant_lookup_quotes_and_escapes_special_character_skus(shopify, transport):
    sku = 'A B:C(D)"E\\F'
    transport.pages = [variant_page(sku, 11)]

    assert shopify.find_variants_by_skus({sku}) == {sku: [variant(sku, 11)]}
    assert transport.last_variables == {"query": 'sku:"A\\ B\\:C\\(D\\)\\"E\\\\F"', "after": None}


def test_get_available_quantity_requires_one_exact_variant(shopify, transport):
    transport.pages = [variant_page("ABC", 11)]

    assert shopify.get_available_quantity("ABC") == 7

    transport.pages = [
        {
            "productVariants": {
                "nodes": [
                    {"id": "gid://shopify/ProductVariant/11", "sku": "ABC", "inventoryQuantity": 7, "inventoryItem": {"tracked": True}},
                    {"id": "gid://shopify/ProductVariant/22", "sku": "ABC", "inventoryQuantity": 4, "inventoryItem": {"tracked": True}},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    ]
    with pytest.raises(ReviewRequiredError, match="exactly one"):
        shopify.get_available_quantity("ABC")


def test_get_available_quantity_requires_a_shopify_inventory_value(shopify, transport):
    transport.pages = [
        {
            "productVariants": {
                "nodes": [
                    {
                        "id": "gid://shopify/ProductVariant/11",
                        "sku": "ABC",
                        "inventoryQuantity": None,
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    ]

    with pytest.raises(ReviewRequiredError, match="inventory quantity"):
        shopify.get_available_quantity("ABC")


@pytest.mark.parametrize("tracked", [True, False])
def test_variant_lookup_exposes_inventory_tracking(shopify, transport, tracked):
    page = variant_page("ABC", 11)
    page["productVariants"]["nodes"][0]["inventoryItem"] = {"tracked": tracked}
    transport.pages = [page]

    result = shopify.find_variants_by_skus(["ABC"])

    assert result["ABC"][0].inventory_tracked is tracked
    assert "inventoryItem { tracked }" in transport.last_query


@pytest.mark.parametrize("inventory_item", [None, {}, {"tracked": "false"}])
def test_variant_lookup_rejects_unknown_tracking(shopify, transport, inventory_item):
    page = variant_page("ABC", 11)
    page["productVariants"]["nodes"][0]["inventoryItem"] = inventory_item
    transport.pages = [page]

    with pytest.raises(ReviewRequiredError, match="tracking"):
        shopify.find_variants_by_skus(["ABC"])


def test_import_handler_creates_then_updates_one_tagged_review(db, shopify, transport):
    from types import SimpleNamespace

    from stock_sync.handlers import handle_import_meli_order

    page = variant_page("ABC", 11)
    page["productVariants"]["nodes"][0]["inventoryItem"]["tracked"] = False
    transport.pages = [page, page]
    meli = SimpleNamespace(get_order=lambda order_id: meli_order())
    job_id = db.enqueue_job("import_meli_order", "meli-order:2001", {"order_id": "2001"}, "order:2001")
    job = db.get_job(job_id)

    handle_import_meli_order(job, db, shopify, meli)
    transport.replies["find_review"] = {"draftOrders": {"nodes": [
        {"id": "gid://shopify/DraftOrder/91", "tags": ["mercadolibre", "meli-needs-review", "meli-review-2001"]}
    ]}}
    handle_import_meli_order(job, db, shopify, meli)

    mutations = [(operation, variables) for operation, variables in transport.calls
                 if operation in {"create_review", "update_review", "create_imported_order"}]
    assert [operation for operation, _ in mutations] == ["create_review", "update_review"]
    assert mutations[0][1]["input"]["tags"] == ["mercadolibre", "meli-needs-review", "meli-review-2001"]
    assert mutations[1][1]["id"] == "gid://shopify/DraftOrder/91"
    assert db.count("review_links") == 1
    assert db.count("order_links") == 0
    assert db.get_job(job_id).status == "needs_review"


def test_user_errors_raise_review_error(shopify, transport):
    transport.reply_with_user_error("lineItems", "Variant is invalid")

    with pytest.raises(ReviewRequiredError) as raised:
        shopify.create_imported_order(meli_order(), {"ABC": variant("ABC")})

    assert raised.value.review_key == "order:2001"
    assert raised.value.details == {
        "operation": "create_imported_order",
        "fields": ["lineItems"],
    }


def test_create_or_update_review_uses_one_safe_draft_and_stable_tag(shopify, transport):
    draft_id = shopify.create_or_update_review("order:2001", "Variant ABC is ambiguous")

    assert draft_id == "gid://shopify/DraftOrder/91"
    assert [operation for operation, _ in transport.calls] == ["find_review", "create_review"]
    variables = transport.last_variables
    assert variables["input"] == {
        "lineItems": [
            {
                "title": "Mercado Libre order needs review",
                "quantity": 1,
                "originalUnitPrice": "0",
            }
        ],
        "note": "Variant ABC is ambiguous",
        "tags": ["mercadolibre", "meli-needs-review", "meli-review-2001"],
    }


def test_existing_review_draft_is_updated_not_recreated(shopify, transport):
    transport.replies["find_review"] = {
        "draftOrders": {"nodes": [{"id": "gid://shopify/DraftOrder/91", "tags": ["mercadolibre"]}]}
    }

    assert shopify.create_or_update_review("order:2001", "Updated note") == "gid://shopify/DraftOrder/91"
    assert [operation for operation, _ in transport.calls] == ["find_review", "update_review"]
    assert transport.last_variables["input"] == {
        "note": "Updated note",
        "tags": ["mercadolibre", "meli-needs-review", "meli-review-2001"],
    }
    assert transport.last_variables["id"] == "gid://shopify/DraftOrder/91"
    assert "$id: ID!" in transport.last_query
    assert "draftOrderUpdate(id: $id, input: $input)" in transport.last_query


def test_resolve_review_replaces_needs_review_tag(shopify, transport):
    transport.replies["find_review"] = {
        "draftOrders": {
            "nodes": [
                {
                    "id": "gid://shopify/DraftOrder/91",
                    "tags": ["mercadolibre", "meli-needs-review", "meli-review-2001"],
                }
            ]
        }
    }

    shopify.resolve_review("order:2001")

    assert transport.last_operation == "resolve_review"
    assert transport.last_variables["id"] == "gid://shopify/DraftOrder/91"
    assert "id" not in transport.last_variables["input"]
    assert "$id: ID!" in transport.last_query
    assert "draftOrderUpdate(id: $id, input: $input)" in transport.last_query
    assert transport.last_variables["input"]["tags"] == [
        "mercadolibre",
        "meli-review-2001",
        "meli-review-resolved",
    ]


def test_mark_order_review_updates_existing_real_order(shopify, transport):
    shopify.mark_order_review("gid://shopify/Order/77", "order:2001", "Needs human review")

    assert [operation for operation, _ in transport.calls] == ["find_order", "mark_order_review"]
    assert transport.last_operation == "mark_order_review"
    assert transport.last_variables["input"] == {
        "id": "gid://shopify/Order/77",
        "note": "Existing order note\n\nNeeds human review",
        "tags": ["existing-tag", "mercadolibre", "meli-needs-review", "meli-review-2001"],
    }


def test_transport_classifies_retryable_and_reviewable_http_failures(monkeypatch):
    from stock_sync.shopify import GraphQLTransport

    class Response:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "request failed"

        def json(self) -> dict[str, Any]:
            return self._payload

    class Session:
        def __init__(self, response: Response) -> None:
            self.response = response

        def post(self, *args, **kwargs) -> Response:
            return self.response

    settings = Settings(
        shopify_shop_url="https://example.myshopify.com/",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        meli_app_id="meli-app-id",
        meli_client_secret="meli-secret",
        meli_expected_seller_id="100",
        meli_webhook_token="meli-webhook-token",
        shopify_api_version="2026-07",
        max_webhook_bytes=1024,
        database_path="stock_sync.db",
    )

    with pytest.raises(RetryableSyncError):
        GraphQLTransport(settings, session=Session(Response(429, {}))).execute("test", "query", {})
    with pytest.raises(RetryableSyncError):
        GraphQLTransport(settings, session=Session(Response(503, {}))).execute("test", "query", {})
    with pytest.raises(ReviewRequiredError) as raised:
        GraphQLTransport(settings, session=Session(Response(401, {}))).execute("test", "query", {})

    assert raised.value.details == {"operation": "test", "fields": []}

    import requests

    class FailingSession:
        def post(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    with pytest.raises(RetryableSyncError, match="offline"):
        GraphQLTransport(settings, session=FailingSession()).execute("test", "query", {})


@pytest.mark.parametrize("code", ["THROTTLED", "INTERNAL_SERVER_ERROR"])
def test_transport_retries_transient_top_level_graphql_errors(code):
    from stock_sync.shopify import GraphQLTransport

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "errors": [
                    {"message": "Shopify is temporarily busy", "extensions": {"code": code}}
                ]
            }

    class Session:
        @staticmethod
        def post(*args, **kwargs) -> Response:
            return Response()

    settings = Settings(
        shopify_shop_url="https://example.myshopify.com",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        meli_app_id="meli-app-id",
        meli_client_secret="meli-secret",
        meli_expected_seller_id="100",
        meli_webhook_token="meli-webhook-token",
        shopify_api_version="2026-07",
        max_webhook_bytes=1024,
        database_path="stock_sync.db",
    )

    with pytest.raises(RetryableSyncError, match="temporarily busy"):
        GraphQLTransport(settings, session=Session()).execute("test", "query", {})


def test_transport_preserves_permanent_top_level_graphql_error_message():
    from stock_sync.shopify import GraphQLTransport

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "errors": [
                    {
                        "message": "Access denied for write_orders",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            }

    class Session:
        @staticmethod
        def post(*args, **kwargs) -> Response:
            return Response()

    settings = Settings(
        shopify_shop_url="https://example.myshopify.com",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        meli_app_id="meli-app-id",
        meli_client_secret="meli-secret",
        meli_expected_seller_id="100",
        meli_webhook_token="meli-webhook-token",
        shopify_api_version="2026-07",
        max_webhook_bytes=1024,
        database_path="stock_sync.db",
    )

    with pytest.raises(ReviewRequiredError, match="Access denied for write_orders"):
        GraphQLTransport(settings, session=Session()).execute("test", "query", {})


def test_transport_posts_to_the_configured_2026_07_graphql_endpoint():
    from stock_sync.shopify import GraphQLTransport

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"data": {"shop": {"id": "gid://shopify/Shop/1"}}}

    class Session:
        def __init__(self) -> None:
            self.url: str | None = None
            self.headers: dict[str, str] | None = None

        def post(self, url: str, *, headers: dict[str, str], **kwargs: Any) -> Response:
            self.url = url
            self.headers = headers
            return Response()

    settings = Settings(
        shopify_shop_url="https://example.myshopify.com/",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        meli_app_id="meli-app-id",
        meli_client_secret="meli-secret",
        meli_expected_seller_id="100",
        meli_webhook_token="meli-webhook-token",
        shopify_api_version="2026-07",
        max_webhook_bytes=1024,
        database_path="stock_sync.db",
    )
    session = Session()

    GraphQLTransport(settings, session=session).execute("test", "query", {})

    assert session.url == "https://example.myshopify.com/admin/api/2026-07/graphql.json"
    assert session.headers == {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": "token",
    }
