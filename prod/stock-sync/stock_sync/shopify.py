from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

import requests

from .config import Settings
from .errors import RetryableSyncError, ReviewRequiredError
from .models import MeliOrder, ShopifyVariant


class GraphQLExecutor(Protocol):
    def execute(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class GraphQLTransport:
    """Small authenticated transport for the Shopify Admin GraphQL API."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.endpoint = (
            f"{settings.shopify_shop_url.rstrip('/')}/admin/api/"
            f"{settings.shopify_api_version}/graphql.json"
        )
        self.access_token = settings.shopify_access_token
        self.session = session or requests.Session()

    def execute(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": self.access_token,
                },
                json={"query": query, "variables": variables},
                timeout=30,
            )
        except requests.RequestException as error:
            raise RetryableSyncError(str(error)) from error

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableSyncError(f"Shopify {operation} failed with HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} authentication failed with HTTP {response.status_code}",
                {"operation": operation, "fields": []},
            )
        if response.status_code >= 400:
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} failed with HTTP {response.status_code}",
                {"operation": operation, "fields": []},
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} returned invalid JSON",
                {"operation": operation, "fields": []},
            ) from error

        if not isinstance(payload, dict):
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} returned an invalid response",
                {"operation": operation, "fields": []},
            )
        if payload.get("errors"):
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} returned GraphQL errors",
                {"operation": operation, "fields": []},
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ReviewRequiredError(
                operation,
                f"Shopify {operation} returned no data",
                {"operation": operation, "fields": []},
            )
        return data


class ShopifyClient:
    def __init__(self, settings: Settings, transport: GraphQLExecutor | None = None) -> None:
        self.transport = transport or GraphQLTransport(settings)

    def find_variants_by_skus(self, skus: Iterable[str]) -> dict[str, list[ShopifyVariant]]:
        matches = {sku: [] for sku in skus}
        for sku in matches:
            after: str | None = None
            while True:
                page = self.transport.execute(
                    "find_variants",
                    """
                    query FindVariants($query: String!, $after: String) {
                      productVariants(first: 100, query: $query, after: $after) {
                        nodes { id sku inventoryQuantity }
                        pageInfo { hasNextPage endCursor }
                      }
                    }
                    """,
                    {"query": f"sku:{sku}", "after": after},
                )["productVariants"]
                for node in page.get("nodes", []):
                    if node.get("sku") == sku:
                        if node.get("inventoryQuantity") is None:
                            raise ReviewRequiredError(
                                f"sku:{sku}",
                                f"Shopify SKU {sku!r} has no inventory quantity",
                                {"operation": "find_variants", "fields": ["inventoryQuantity"]},
                            )
                        matches[sku].append(
                            ShopifyVariant(
                                variant_id=str(node["id"]),
                                sku=sku,
                                available_quantity=int(node["inventoryQuantity"]),
                            )
                        )
                page_info = page.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
                if not after:
                    raise ReviewRequiredError(
                        f"sku:{sku}",
                        "Shopify variant pagination did not include an end cursor",
                        {"operation": "find_variants", "fields": []},
                    )
        return matches

    def get_available_quantity(self, sku: str) -> int:
        variants = self.find_variants_by_skus({sku}).get(sku, [])
        if len(variants) != 1:
            raise ReviewRequiredError(
                f"sku:{sku}",
                f"Shopify SKU {sku!r} must match exactly one variant, found {len(variants)}",
                {"operation": "get_available_quantity", "fields": ["sku"]},
            )
        return variants[0].available_quantity

    def find_imported_order(self, meli_order_id: str) -> str | None:
        data = self.transport.execute(
            "find_imported_order",
            """
            query FindImportedOrder($query: String!) {
              orders(first: 1, query: $query) { nodes { id } }
            }
            """,
            {"query": f"tag:meli-order-{meli_order_id}"},
        )
        nodes = data["orders"].get("nodes", [])
        return None if not nodes else str(nodes[0]["id"])

    def create_imported_order(
        self, order: MeliOrder, variants: Mapping[str, ShopifyVariant]
    ) -> str:
        existing = self.find_imported_order(order.order_id)
        if existing:
            return existing

        line_items = []
        for line in order.lines:
            variant = variants.get(line.sku)
            if variant is None:
                raise ReviewRequiredError(
                    f"order:{order.order_id}",
                    f"No Shopify variant was selected for Mercado Libre SKU {line.sku!r}",
                    {"operation": "create_imported_order", "fields": ["lineItems"]},
                )
            line_items.append(
                {
                    "variantId": variant.variant_id,
                    "quantity": line.quantity,
                    "priceSet": {
                        "shopMoney": {"amount": line.unit_price, "currencyCode": line.currency}
                    },
                }
            )

        data = self.transport.execute(
            "create_imported_order",
            """
            mutation CreateImportedOrder($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
              orderCreate(order: $order, options: $options) {
                order { id }
                userErrors { field message }
              }
            }
            """,
            {
                "order": {
                    "processedAt": order.processed_at,
                    "financialStatus": "PAID",
                    "sourceIdentifier": order.order_id,
                    "tags": ["mercadolibre", f"meli-order-{order.order_id}"],
                    "note": f"Imported from Mercado Libre order {order.order_id}.",
                    "lineItems": line_items,
                },
                "options": {
                    "inventoryBehaviour": "DECREMENT_IGNORING_POLICY",
                    "sendReceipt": False,
                    "sendFulfillmentReceipt": False,
                },
            },
        )
        result = self._mutation_result("create_imported_order", data, "orderCreate", f"order:{order.order_id}")
        return str(result["order"]["id"])

    def create_or_update_review(self, review_key: str, note: str) -> str:
        stable_tag = self._review_tag(review_key)
        existing = self._find_review(stable_tag)
        tags = self._review_tags(stable_tag)
        if existing is None:
            data = self.transport.execute(
                "create_review",
                """
                mutation CreateReview($input: DraftOrderInput!) {
                  draftOrderCreate(input: $input) {
                    draftOrder { id }
                    userErrors { field message }
                  }
                }
                """,
                {"input": self._review_draft_input(note, tags)},
            )
            result = self._mutation_result("create_review", data, "draftOrderCreate", review_key)
            return str(result["draftOrder"]["id"])

        draft_id, old_tags = existing
        data = self.transport.execute(
            "update_review",
            """
            mutation UpdateReview($input: DraftOrderInput!) {
              draftOrderUpdate(input: $input) {
                draftOrder { id }
                userErrors { field message }
              }
            }
            """,
            {
                "input": {
                    "id": draft_id,
                    "note": note,
                    "tags": self._merged_tags(old_tags, tags),
                }
            },
        )
        result = self._mutation_result("update_review", data, "draftOrderUpdate", review_key)
        return str(result["draftOrder"]["id"])

    def resolve_review(self, review_key: str) -> None:
        stable_tag = self._review_tag(review_key)
        existing = self._find_review(stable_tag)
        if existing is None:
            return
        draft_id, old_tags = existing
        tags = [tag for tag in old_tags if tag != "meli-needs-review"]
        tags = self._merged_tags(tags, ["meli-review-resolved"])
        data = self.transport.execute(
            "resolve_review",
            """
            mutation ResolveReview($input: DraftOrderInput!) {
              draftOrderUpdate(input: $input) {
                draftOrder { id }
                userErrors { field message }
              }
            }
            """,
            {"input": {"id": draft_id, "tags": tags}},
        )
        self._mutation_result("resolve_review", data, "draftOrderUpdate", review_key)

    def mark_order_review(self, order_id: str, review_key: str, note: str) -> None:
        stable_tag = self._review_tag(review_key)
        existing_tags, existing_note = self._find_order(order_id)
        data = self.transport.execute(
            "mark_order_review",
            """
            mutation MarkOrderReview($input: OrderInput!) {
              orderUpdate(input: $input) {
                order { id }
                userErrors { field message }
              }
            }
            """,
            {
                "input": {
                    "id": order_id,
                    "note": self._append_note(existing_note, note),
                    "tags": self._merged_tags(existing_tags, self._review_tags(stable_tag)),
                }
            },
        )
        self._mutation_result("mark_order_review", data, "orderUpdate", review_key)

    def _find_review(self, stable_tag: str) -> tuple[str, list[str]] | None:
        data = self.transport.execute(
            "find_review",
            """
            query FindReview($query: String!) {
              draftOrders(first: 1, query: $query) { nodes { id tags } }
            }
            """,
            {"query": f"tag:{stable_tag}"},
        )
        nodes = data["draftOrders"].get("nodes", [])
        if not nodes:
            return None
        return str(nodes[0]["id"]), [str(tag) for tag in nodes[0].get("tags", [])]

    def _find_order(self, order_id: str) -> tuple[list[str], str]:
        data = self.transport.execute(
            "find_order",
            """
            query FindOrder($id: ID!) { node(id: $id) { ... on Order { id note tags } } }
            """,
            {"id": order_id},
        )
        node = data.get("node")
        if not isinstance(node, dict) or node.get("id") != order_id:
            raise ReviewRequiredError(
                order_id,
                "Shopify order was not found before adding review tags",
                {"operation": "mark_order_review", "fields": ["id"]},
            )
        return [str(tag) for tag in node.get("tags", [])], str(node.get("note") or "")

    @staticmethod
    def _review_tag(review_key: str) -> str:
        return f"meli-review-{review_key.split(':', 1)[-1]}"

    @staticmethod
    def _review_tags(stable_tag: str) -> list[str]:
        return ["mercadolibre", "meli-needs-review", stable_tag]

    @staticmethod
    def _review_draft_input(note: str, tags: list[str]) -> dict[str, Any]:
        return {
            "lineItems": [
                {
                    "title": "Mercado Libre order needs review",
                    "quantity": 1,
                    "originalUnitPrice": "0",
                }
            ],
            "note": note,
            "tags": tags,
        }

    @staticmethod
    def _merged_tags(existing: Iterable[str], additions: Iterable[str]) -> list[str]:
        return list(dict.fromkeys([*existing, *additions]))

    @staticmethod
    def _append_note(existing: str, note: str) -> str:
        if not existing or note in existing:
            return existing or note
        return f"{existing}\n\n{note}"

    @staticmethod
    def _mutation_result(
        operation: str, data: dict[str, Any], mutation_name: str, review_key: str
    ) -> dict[str, Any]:
        result = data.get(mutation_name)
        if not isinstance(result, dict):
            raise ReviewRequiredError(
                review_key,
                f"Shopify {operation} returned no mutation result",
                {"operation": operation, "fields": []},
            )
        errors = result.get("userErrors") or []
        if errors:
            fields = [str(field) for error in errors for field in error.get("field") or []]
            message = "; ".join(str(error.get("message", "Unknown Shopify error")) for error in errors)
            raise ReviewRequiredError(
                review_key,
                f"Shopify {operation}: {message}",
                {"operation": operation, "fields": fields},
            )
        return result
