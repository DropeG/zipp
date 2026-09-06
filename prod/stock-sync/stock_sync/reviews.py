"""Persisted review identities shared by handlers, daily recovery and worker."""
from .db import Database
from .shopify import ShopifyClient


def publish_draft(db: Database, shopify: ShopifyClient, key: str, note: str) -> None:
    draft_id = shopify.create_or_update_review(key, note, draft_id=db.get_review_link(key))
    db.link_review(key, draft_id)


def resolve_draft(db: Database, shopify: ShopifyClient, key: str, order_id: str | None = None) -> None:
    shopify.resolve_review(key, draft_id=db.get_review_link(key), shopify_order_id=order_id)


def mark_order(db: Database, shopify: ShopifyClient, order_id: str, key: str, note: str) -> None:
    # Keep the intent if publication or the following local write is interrupted.
    db.record_order_review(order_id, key)
    shopify.mark_order_review(order_id, key, note)


def resolve_order(db: Database, shopify: ShopifyClient, order_id: str, key: str) -> None:
    if db.has_order_review(order_id, key):
        shopify.resolve_order_review(order_id, key)
        db.clear_order_review(order_id, key)
