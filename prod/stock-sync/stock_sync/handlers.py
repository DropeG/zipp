from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .db import Database
from .errors import ReviewRequiredError
from .meli import MeliClient
from .models import Job, MeliOrder, ShopifyVariant
from .shopify import ShopifyClient


def _validate_import(
    order: MeliOrder, shopify: ShopifyClient,
) -> tuple[dict[str, ShopifyVariant], list[str]]:
    problems = []
    skus = set()
    currencies = set()
    if not order.lines:
        problems.append("Order must contain at least one line")
    for index, line in enumerate(order.lines, 1):
        if not isinstance(line.sku, str) or not line.sku.strip():
            problems.append(f"Line {index}: missing SKU")
        else:
            skus.add(line.sku)
        if type(line.quantity) is not int or line.quantity <= 0:
            problems.append(f"Line {index}: quantity must be a positive integer")
        try:
            amount = Decimal(line.unit_price)
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid price")
        except (InvalidOperation, TypeError, ValueError):
            problems.append(f"Line {index}: invalid unit price")
        if not isinstance(line.currency, str) or not line.currency.strip():
            problems.append(f"Line {index}: missing currency")
        else:
            currencies.add(line.currency)
    if len(currencies) > 1:
        problems.append("Order lines must use one currency")

    variants = {}
    # Look up each SKU independently so one permanent lookup error cannot hide
    # validation problems for the remaining lines. Temporary errors still retry.
    for sku in sorted(skus):
        try:
            matches = [
                variant for variant in shopify.find_variants_by_skus([sku]).get(sku, [])
                if variant.sku == sku
            ]
        except ReviewRequiredError as error:
            problems.append(f"SKU {sku!r}: {error}")
            continue
        if len(matches) != 1:
            problems.append(f"SKU {sku!r}: must match exactly one Shopify variant, found {len(matches)}")
        elif matches[0].inventory_tracked is not True:
            problems.append(f"SKU {sku!r}: Shopify inventory must be tracked")
        else:
            variants[sku] = matches[0]
    return variants, problems


def _import_review(
    job: Job, order_id: str, problems: list[str], db: Database,
    shopify: ShopifyClient, dry_run: bool,
) -> dict[str, Any]:
    review_key = f"order:{order_id}"
    if not dry_run:
        note = "\n".join(problems)
        draft_id = shopify.create_or_update_review(review_key, note)
        db.link_review(review_key, draft_id)
        db.needs_review(job.id, review_key, note)
    return {"status": "needs_review", "order_id": order_id, "problems": problems}


def handle_import_meli_order(
    job: Job, db: Database, shopify: ShopifyClient, meli: MeliClient,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import one trusted paid order; the worker owns success and retry status.

    Dry run permits authenticated reads, including OAuth refresh plumbing, but
    never mutates business records. Processing is serialized by the job's order
    resource key; persistent links and Shopify lookup recover interrupted work.
    """
    order_id = str(job.payload["order_id"])
    try:
        order = meli.get_order(order_id)
    except ReviewRequiredError as error:
        return _import_review(job, order_id, [str(error)], db, shopify, dry_run)

    if order.status != "paid":
        return {"status": "skipped", "order_id": order_id, "reason": f"Order status is {order.status}"}

    variants, problems = _validate_import(order, shopify)
    if problems:
        return _import_review(job, order_id, problems, db, shopify, dry_run)

    try:
        shopify_order_id = db.get_order_link(order_id)
        if shopify_order_id is None:
            shopify_order_id = shopify.find_imported_order(order_id)

        if dry_run:
            return {
                "status": "dry_run",
                "order_id": order_id,
                "shopify_order_id": shopify_order_id,
                "lines": [
                    {"sku": line.sku, "variant_id": variants[line.sku].variant_id,
                     "quantity": line.quantity, "unit_price": line.unit_price, "currency": line.currency}
                    for line in order.lines
                ],
                "tags": ["mercadolibre", f"meli-order-{order_id}"],
                "inventory_behaviour": "DECREMENT_IGNORING_POLICY",
            }

        if shopify_order_id is None:
            shopify_order_id = shopify.create_imported_order(order, variants)
    except ReviewRequiredError as error:
        return _import_review(job, order_id, [str(error)], db, shopify, dry_run)

    db.link_order(order_id, shopify_order_id)
    # Shopify webhooks carry numeric IDs; GraphQL returns global IDs. Both paths
    # must produce the same source key, including after a partial enqueue crash.
    numeric_order_id = shopify_order_id.rsplit("/", 1)[-1]
    for sku in sorted(variants):
        db.enqueue_job(
            "reconcile_sku", f"shopify-order:{numeric_order_id}:{sku}",
            {"sku": sku, "shopify_order_id": shopify_order_id}, f"sku:{sku}",
        )
    return {"status": "imported", "order_id": order_id, "shopify_order_id": shopify_order_id}


def handle_shopify_order(
    job: Job, db: Database, shopify: ShopifyClient,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Queue exact SKUs from an order whose inventory Shopify already handled."""
    numeric_order_id = str(job.payload["id"])
    shopify_order_id = f"gid://shopify/Order/{numeric_order_id}"
    lines = job.payload.get("line_items")
    problems = []
    skus = set()
    if not isinstance(lines, list) or not lines:
        problems.append("Order must contain line items with SKUs")
    else:
        for index, line in enumerate(lines, 1):
            sku = line.get("sku") if isinstance(line, dict) else None
            if not isinstance(sku, str) or not sku.strip():
                problems.append(f"Line {index}: missing SKU")
            else:
                skus.add(sku)

    for sku in sorted(skus):
        if not dry_run:
            db.enqueue_job(
                "reconcile_sku", f"shopify-order:{numeric_order_id}:{sku}",
                {"sku": sku, "shopify_order_id": shopify_order_id}, f"sku:{sku}",
            )

    if problems:
        review_key = f"shopify-order:{numeric_order_id}"
        note = "\n".join(problems)
        if not dry_run:
            shopify.mark_order_review(shopify_order_id, review_key, note)
            db.needs_review(job.id, review_key, note)
        return {"status": "needs_review", "shopify_order_id": shopify_order_id, "problems": problems, "skus": sorted(skus)}
    return {"status": "dry_run" if dry_run else "enqueued", "shopify_order_id": shopify_order_id, "skus": sorted(skus)}


def handle_reconcile_sku(
    job: Job, db: Database, shopify: ShopifyClient, meli: MeliClient,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy fresh Shopify stock to one exact listing; never subtract inventory.

    The worker serializes jobs. Catalog resolution precedes the Shopify read so
    a slow listing scan cannot leave a cached Shopify quantity at write time.
    """
    sku = job.payload["sku"]
    shopify_order_id = job.payload["shopify_order_id"]
    review_key = f"sku:{sku}"
    try:
        if not isinstance(sku, str) or not sku.strip():
            raise ReviewRequiredError(review_key, "Reconciliation requires a non-empty SKU", {})
        matches = [listing for listing in meli.list_all_listings() if listing.sku == sku]
        if len(matches) != 1:
            raise ReviewRequiredError(
                review_key,
                f"Mercado Libre SKU {sku!r} must match exactly one item or variation, found {len(matches)}",
                {"sku": sku},
            )
        listing = matches[0]
        shopify_quantity = shopify.get_available_quantity(sku)
        # Negative Shopify inventory represents overselling; Mercado Libre's
        # sellable quantity is zero, matching its client's write contract.
        target_quantity = max(0, shopify_quantity)
        changed = listing.available_quantity != target_quantity
        if changed and not dry_run:
            meli.set_available_quantity(listing, target_quantity)
    except ReviewRequiredError as error:
        # Keep review failures outside the API catch boundary: a failed notice
        # must propagate, never be swallowed or attempted twice in this call.
        problem = str(error)
    else:
        return {
            "status": "dry_run" if dry_run else ("updated" if changed else "unchanged"),
            "sku": sku,
            "shopify_order_id": shopify_order_id,
            "shopify_quantity": shopify_quantity,
            "meli_quantity": listing.available_quantity,
            "target_quantity": target_quantity,
        }

    if not dry_run:
        note = f"SKU {sku!r}: {problem}"
        shopify.mark_order_review(shopify_order_id, review_key, note)
        db.needs_review(job.id, review_key, note)
    return {"status": "needs_review", "sku": sku, "shopify_order_id": shopify_order_id, "problems": [problem]}
