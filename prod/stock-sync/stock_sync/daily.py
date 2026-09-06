from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from .db import Database
from .errors import ReviewRequiredError
from .handlers import handle_import_meli_order
from .meli import MeliClient
from .models import Job, MeliListing, ShopifyVariant
from .reviews import publish_draft, resolve_draft
from .shopify import ShopifyClient


@dataclass
class DailyResult:
    status: str = "completed"
    recovered: int = 0
    updated: int = 0
    unchanged: int = 0
    review_keys: list[str] = field(default_factory=list)
    planned_updates: list[dict] = field(default_factory=list)


def _product_review_key(kind: str, identity: str) -> str:
    # The existing Shopify review client embeds the suffix in a tag search.
    # Bound its size and avoid treating exact SKU punctuation as search syntax.
    return f"product:{kind}-{sha256(identity.encode('utf-8')).hexdigest()}"


def _entity_review_key(entity: ShopifyVariant | MeliListing) -> str:
    if isinstance(entity, ShopifyVariant):
        return _product_review_key("shopify-variant", entity.variant_id)
    return _product_review_key("meli-listing", f"{entity.item_id}:{entity.variation_id or ''}")


def run_daily(
    db: Database, shopify: ShopifyClient, meli: MeliClient,
    now: datetime, dry_run: bool = False,
) -> DailyResult:
    """Recover paid orders, then reconcile full catalogs in the single worker.

    An unresolved import blocks the product phase: Shopify must include every
    recovered sale before its quantity can safely be copied to Mercado Libre.
    Dry runs perform reads only and never advance the recovery checkpoint.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    result = DailyResult(status="dry_run" if dry_run else "completed")
    # Review publication belongs to the queued worker. Do not bypass its
    # durable stop condition, even when an order falls outside today's search.
    # Published import reviews also require an explicit retry before recovery.
    with db._connect() as connection:
        blocked = connection.execute(
            """SELECT resource_key FROM jobs
               WHERE job_type = 'import_meli_order'
                 AND (status = 'needs_review' OR EXISTS (
                     SELECT 1 FROM checkpoints
                     WHERE checkpoint_key = 'worker_review:' || jobs.id))
               ORDER BY id""",
        ).fetchall()
    if blocked:
        result.status = "needs_review"
        result.review_keys = [row["resource_key"] for row in blocked]
        return result

    checkpoint = db.get_checkpoint("daily_orders_completed_at")
    since = datetime.fromisoformat(checkpoint) - timedelta(days=1) if checkpoint else now - timedelta(days=30)

    for order_id in meli.search_paid_orders(since):
        source_key = f"meli-order:{order_id}"
        resource_key = f"order:{order_id}"
        payload = {"order_id": order_id}
        if dry_run:
            job = Job(0, "import_meli_order", source_key, resource_key, payload, "pending", 0, now.isoformat(), None)
        else:
            job_id = db.enqueue_job("import_meli_order", source_key, payload, resource_key)
            job = db.get_job(job_id)
        imported = handle_import_meli_order(job, db, shopify, meli, dry_run=dry_run)
        if imported["status"] == "needs_review":
            result.status = "needs_review"
            result.review_keys.append(resource_key)
            return result
        if not dry_run:
            db.complete_job(job.id)
        if imported["status"] in {"imported", "dry_run"}:
            result.recovered += 1

    if not dry_run:
        db.set_checkpoint("daily_orders_completed_at", now.isoformat())

    variants = shopify.list_all_variants()
    listings = meli.list_all_listings()
    managed_skus = {v.sku for v in variants if v.inventory_tracked is True}
    listed_skus = {item.sku for item in listings}
    variants = [v for v in variants if v.inventory_tracked is True or
                (v.sku and v.sku.strip() and v.sku in managed_skus | listed_skus)]
    shopify_skus = defaultdict(list)
    meli_skus = defaultdict(list)

    def review(key: str, note: str) -> None:
        if not dry_run:
            publish_draft(db, shopify, key, note)
        result.review_keys.append(key)
        result.status = "needs_review"

    for catalog, index in ((variants, shopify_skus), (listings, meli_skus)):
        for entity in catalog:
            if not isinstance(entity.sku, str) or not entity.sku.strip():
                review(_entity_review_key(entity), f"Missing or blank SKU: {entity}")
            else:
                index[entity.sku].append(entity)

    for sku in sorted(shopify_skus.keys() | meli_skus.keys()):
        matches = shopify_skus[sku]
        targets = meli_skus[sku]
        key = _product_review_key("sku", sku)
        if len(matches) != 1 or len(targets) != 1:
            review(key, f"SKU {sku!r} requires exactly one Shopify variant and one Mercado Libre item/variation; found {len(matches)} and {len(targets)}")
            continue
        if matches[0].inventory_tracked is not True:
            review(key, f"SKU {sku!r}: Shopify inventory must be tracked")
            continue
        target = targets[0]
        try:
            # A full scan can be slow. Read Shopify again immediately before
            # comparing/writing, as in the order-driven reconciliation handler.
            quantity = max(0, shopify.get_available_quantity(sku))
            changed = target.available_quantity != quantity
            if changed and not dry_run:
                meli.set_available_quantity(target, quantity)
        except ReviewRequiredError as error:
            review(key, f"SKU {sku!r}: {error}")
            continue
        if changed:
            result.planned_updates.append({"sku": sku, "item_id": target.item_id,
                                           "variation_id": target.variation_id, "quantity": quantity})
            if not dry_run:
                result.updated += 1
        else:
            result.unchanged += 1
        if not dry_run:
            # SKU reviews remain stable across changes in problem type. Entity
            # reviews also resolve when a previously blank SKU is repaired.
            for review_key in (key, _entity_review_key(matches[0]), _entity_review_key(target)):
                resolve_draft(db, shopify, review_key)

    return result
