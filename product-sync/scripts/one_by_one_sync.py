#!/usr/bin/env python3
"""Safe entry point for the one-product Shopify -> Mercado Libre workflow."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for import_path in (REPO_ROOT, SCRIPT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from one_by_one_contract import (  # noqa: E402
    BLOCKED_PRODUCTS_FILE,
    DEFAULT_WORK_DIR,
    MAPPINGS_FILE,
    PUBLICATION_JOURNAL_FILE,
    QUALITY_RECORDS_FILE,
    RESERVATIONS_FILE,
    SCHEMA_VERSION,
    WORKFLOW_LOCK_FILE,
    assert_prepared_payload,
    atomic_write_json,
    load_blocked_products,
    mapping_item_ids,
    read_json,
    select_first_eligible,
    workflow_lock,
)
from shared.meli_client import (  # noqa: E402
    get_category_attributes,
    get_meli_headers,
    get_meli_user_me,
    predict_meli_category,
)
from shared.shopify_client import get_product_metafield, get_shopify_products  # noqa: E402


MELI_BRAND_METAFIELD_NAMESPACE = "zipp_sync"
MELI_BRAND_METAFIELD_KEY = "meli_brand"
DEFAULT_MELI_BRAND = "Genérica"
MAX_CATEGORY_CANDIDATES = 5


def fetch_meli_item(item_id: str) -> dict[str, Any] | None:
    response = requests.get(
        f"https://api.mercadolibre.com/items/{item_id}",
        headers=get_meli_headers(),
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def find_seller_items_by_sku(seller_id: str, sku: str) -> list[dict[str, Any]]:
    """Find own items through both official legacy and SELLER_SKU filters."""
    item_ids: list[str] = []
    for parameter in ("sku", "seller_sku"):
        response = requests.get(
            f"https://api.mercadolibre.com/users/{seller_id}/items/search",
            headers=get_meli_headers(),
            params={parameter: sku},
            timeout=30,
        )
        response.raise_for_status()
        for item_id in response.json().get("results") or []:
            if str(item_id) not in item_ids:
                item_ids.append(str(item_id))
    return [item for item_id in item_ids if (item := fetch_meli_item(item_id))]


def reconcile_seller_skus(
    product: dict[str, Any], existing_mapping: Any = None
) -> dict[str, Any]:
    seller = get_meli_user_me()
    seller_id = str(seller.get("id") or "")
    if not seller_id:
        raise RuntimeError("Mercado Libre user response has no seller id")
    result: dict[str, Any] = {"seller_id": seller_id, "variants": {}}
    for variant in product.get("variants") or []:
        variant_id = str(variant.get("id") or "")
        sku = str(variant.get("sku") or "").strip()
        matches = find_seller_items_by_sku(seller_id, sku) if sku else []
        result["variants"][variant_id] = {
            "sku": sku,
            "matches": [
                {
                    "meli_item_id": item.get("id"),
                    "title": item.get("title"),
                    "category_id": item.get("category_id"),
                    "status": item.get("status"),
                    "sub_status": item.get("sub_status") or [],
                    "price": item.get("price"),
                    "available_quantity": item.get("available_quantity"),
                    "sold_quantity": item.get("sold_quantity"),
                    "family_id": item.get("family_id"),
                    "user_product_id": item.get("user_product_id"),
                    "seller_custom_field": item.get("seller_custom_field"),
                    "attributes": item.get("attributes") or [],
                    "pictures": item.get("pictures") or [],
                }
                for item in matches
            ],
        }
    return result


def reconcile_mappings(
    mappings: dict[str, Any],
    fetch_item=fetch_meli_item,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove only mappings positively proven missing or deleted."""
    reconciled = dict(mappings)
    removed: list[dict[str, Any]] = []
    for shopify_id, mapping in mappings.items():
        item_ids = mapping_item_ids(mapping)
        # A family mapping remains valid while at least one member survives.
        # Removing a complete family on a transient/partial failure would make
        # the selector publish duplicates.
        deleted_ids: list[str] = []
        for meli_id in item_ids:
            item = fetch_item(str(meli_id))
            is_deleted = item is None
            if item:
                is_deleted = item.get("status") == "deleted" or "deleted" in (
                    item.get("sub_status") or []
                )
            if is_deleted:
                deleted_ids.append(str(meli_id))
        if item_ids and len(deleted_ids) == len(item_ids):
            reconciled.pop(shopify_id, None)
            removed.append(
                {
                    "shopify_id": shopify_id,
                    "meli_ids": deleted_ids,
                    "reason": "missing_or_deleted",
                }
            )
    return reconciled, removed


def unique_category_candidates(
    predictions: list[dict[str, Any]], existing: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Keep at most five distinct Domain Discovery candidates in stable order."""
    candidates = list(existing or [])
    seen = {str(item.get("category_id") or item.get("id") or "") for item in candidates}
    for prediction in predictions:
        category_id = str(prediction.get("category_id") or prediction.get("id") or "")
        if not category_id or category_id in seen:
            continue
        candidates.append(
            {
                "category_id": category_id,
                "category_name": prediction.get("category_name") or prediction.get("name"),
                "domain_id": prediction.get("domain_id"),
            }
        )
        seen.add(category_id)
        if len(candidates) >= MAX_CATEGORY_CANDIDATES:
            break
    return candidates


def choose_best_category(
    product: dict[str, Any], predictions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compatibility helper: return the first candidate, never a hard-coded override."""
    candidates = unique_category_candidates(predictions)
    if not candidates:
        raise ValueError("no category candidates")
    return candidates[0]


def normalize_category_attributes(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for attribute in attributes:
        normalized.append(
            {
                "id": attribute.get("id"),
                "name": attribute.get("name"),
                "value_type": attribute.get("value_type"),
                "tags": attribute.get("tags") or {},
                "values": [
                    {"id": value.get("id"), "name": value.get("name")}
                    for value in attribute.get("values") or []
                ],
            }
        )
    return normalized


def product_policy_hints(product: dict[str, Any]) -> list[str]:
    searchable = " ".join(
        str(product.get(field) or "")
        for field in ("title", "body_html", "product_type", "vendor")
    ).lower()
    hints = []
    if any(term in searchable for term in ("batería", "bateria", "battery", "litio", "lithium", "mah", "power bank")):
        hints.append("shipping_sensitive_battery")
    if any(term in searchable for term in ("perfume", "aerosol", "inflamable", "flammable")):
        hints.append("shipping_sensitive_hazardous")
    if any(term in searchable for term in ("médico", "medico", "salud", "terapia", "vigilancia")):
        hints.append("policy_review_required")
    return hints


def prepare(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).resolve()
    source_path = work_dir / "source.json"
    audit_path = work_dir / "selection_audit.json"
    reservations_path = Path(
        getattr(args, "reservations", RESERVATIONS_FILE)
    ).resolve()
    journal_path = Path(getattr(args, "journal", PUBLICATION_JOURNAL_FILE)).resolve()

    mappings = read_json(Path(args.mappings), {})
    if not isinstance(mappings, dict):
        raise ValueError("sync_mappings.json must contain a JSON object")

    reservations = read_json(reservations_path, {})
    journal = read_json(journal_path, {})
    if not isinstance(reservations, dict) or not isinstance(journal, dict):
        raise ValueError("reservation and journal state must be JSON objects")
    pending_ids = set(reservations) | set(journal)
    if pending_ids:
        if len(pending_ids) != 1:
            raise RuntimeError(
                "multiple pending Shopify products exist; resolve state before selecting"
            )
        pending_id = next(iter(pending_ids))
        existing_source = read_json(source_path, {})
        existing_product = existing_source.get("selected_product") or {}
        if str(existing_product.get("id") or "") != pending_id:
            raise RuntimeError(
                f"Shopify product {pending_id} is reserved but its source file is missing"
            )
        print(
            json.dumps(
                {
                    "outcome": "prepared",
                    "mode": existing_source.get("mode") or args.mode,
                    "shopify_id": pending_id,
                    "source": str(source_path),
                    "payload": str(work_dir / "productos_listos.json"),
                    "resumed_reservation": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    reconciled, removed = reconcile_mappings(mappings)
    if args.mode == "publish" and reconciled != mappings:
        atomic_write_json(Path(args.mappings), reconciled)

    blocked_products = load_blocked_products(Path(args.blocked_products))
    products = get_shopify_products(limit=args.limit).get("products", [])
    selected, skipped = select_first_eligible(products, reconciled, blocked_products)

    audit = {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_products_seen": len(products),
        "removed_stale_mappings": removed,
        "selected_shopify_id": selected.get("id") if selected else None,
        "skipped_before_selection": skipped,
    }
    atomic_write_json(audit_path, audit)

    if not selected:
        atomic_write_json(
            source_path,
            {
                "schema_version": SCHEMA_VERSION,
                "mode": args.mode,
                "selected_product": None,
                "category": None,
                "category_attributes": [],
                "policy_hints": [],
            },
        )
        print(
            json.dumps(
                {
                    "outcome": "no-op",
                    "mode": args.mode,
                    "reason": "No eligible unsynced products found",
                    "source": str(source_path),
                },
                ensure_ascii=False,
            )
        )
        return 0

    # Shopify vendor identifies the seller/storefront, not necessarily the
    # manufacturer. Mercado Libre brand therefore has its own explicit source.
    selected = dict(selected)
    configured_brand = get_product_metafield(
        selected.get("id"),
        MELI_BRAND_METAFIELD_NAMESPACE,
        MELI_BRAND_METAFIELD_KEY,
    )
    configured_brand = str(configured_brand or "").strip()
    selected["meli_brand"] = (
        DEFAULT_MELI_BRAND
        if configured_brand.casefold() in {"", "zipp", "zipp chile", "zipp.cl"}
        else configured_brand
    )

    predictions = predict_meli_category(str(selected.get("title") or ""))
    if not predictions:
        raise RuntimeError("Mercado Libre returned no category prediction")
    candidates = unique_category_candidates(predictions)
    category = candidates[0]
    category_id = category.get("category_id")
    if not category_id:
        raise RuntimeError("The selected category prediction has no category_id")
    attributes = normalize_category_attributes(get_category_attributes(category_id))

    source = {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "selected_product": selected,
        "existing_mapping": reconciled.get(str(selected.get("id"))),
        "sku_reconciliation": reconcile_seller_skus(
            selected, reconciled.get(str(selected.get("id")))
        ),
        "category": {
            "id": category_id,
            "name": category.get("category_name"),
            "domain_id": category.get("domain_id"),
            "selection_reason": "initial_domain_discovery_candidate",
        },
        "category_candidates": candidates,
        "category_resolution": {
            "max_candidates": MAX_CATEGORY_CANDIDATES,
            "queries": [
                {
                    "query": str(selected.get("title") or ""),
                    "reason": "exact_shopify_title",
                }
            ],
            "selections": [
                {
                    "category_id": category_id,
                    "reason": "initial_domain_discovery_candidate",
                }
            ],
        },
        "category_attributes": attributes,
        "policy_hints": product_policy_hints(selected),
    }
    atomic_write_json(source_path, source)
    if args.mode == "publish":
        reservations[str(selected.get("id"))] = {
            "stage": "prepared",
            "reserved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": str(source_path),
        }
        atomic_write_json(reservations_path, reservations)
    print(
        json.dumps(
            {
                "outcome": "prepared",
                "mode": args.mode,
                "shopify_id": str(selected.get("id")),
                "source": str(source_path),
                "payload": str(work_dir / "productos_listos.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def check_payload(args: argparse.Namespace) -> int:
    source = read_json(Path(args.source), {})
    payloads = read_json(Path(args.payload), None)
    try:
        payload = assert_prepared_payload(payloads, source)
    except ValueError as error:
        print(json.dumps({"outcome": "invalid", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "outcome": "valid",
                "shopify_id": str(payload.get("shopify_id")),
                "payload": str(Path(args.payload).resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


def discover_categories(args: argparse.Namespace) -> int:
    """Add source-supported Domain Discovery results to the current run."""
    source_path = Path(args.source).resolve()
    source = read_json(source_path, {})
    if not isinstance(source.get("selected_product"), dict):
        raise ValueError("source file has no selected Shopify product")
    resolution = source.setdefault(
        "category_resolution",
        {"max_candidates": MAX_CATEGORY_CANDIDATES, "queries": [], "selections": []},
    )
    queries = resolution.setdefault("queries", [])
    if len(queries) >= MAX_CATEGORY_CANDIDATES:
        raise ValueError("category query budget exhausted")
    predictions = predict_meli_category(args.query)
    candidates = unique_category_candidates(
        predictions, source.get("category_candidates") or []
    )
    queries.append({"query": args.query, "reason": args.reason})
    source["category_candidates"] = candidates
    atomic_write_json(source_path, source)
    print(
        json.dumps(
            {
                "outcome": "category_candidates_updated",
                "query": args.query,
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            ensure_ascii=False,
        )
    )
    return 0


def select_category(args: argparse.Namespace) -> int:
    """Select one discovered category and persist its current attribute schema."""
    source_path = Path(args.source).resolve()
    source = read_json(source_path, {})
    candidates = source.get("category_candidates") or []
    category = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("category_id") or "") == str(args.category_id)
        ),
        None,
    )
    if not category:
        raise ValueError("category_id is not in the current discovered candidates")
    selections = source.setdefault("category_resolution", {}).setdefault("selections", [])
    tried_ids = {str(selection.get("category_id") or "") for selection in selections}
    if args.category_id not in tried_ids and len(tried_ids) >= MAX_CATEGORY_CANDIDATES:
        raise ValueError("category selection budget exhausted")
    source["category"] = {
        "id": args.category_id,
        "name": category.get("category_name"),
        "domain_id": category.get("domain_id"),
        "selection_reason": args.reason,
    }
    source["category_attributes"] = normalize_category_attributes(
        get_category_attributes(args.category_id)
    )
    selections.append({"category_id": args.category_id, "reason": args.reason})
    atomic_write_json(source_path, source)
    print(
        json.dumps(
            {
                "outcome": "category_selected",
                "category": source["category"],
                "attribute_count": len(source["category_attributes"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def block_product(args: argparse.Namespace) -> int:
    source = read_json(Path(args.source), {})
    selected = source.get("selected_product") or {}
    shopify_id = str(selected.get("id") or "")
    if not shopify_id:
        raise ValueError("source file has no selected Shopify product")
    blockers = load_blocked_products(Path(args.blocked_products))
    previous = blockers.get(shopify_id) or {}
    meli_ids = list(previous.get("meli_item_ids") or [])
    if args.meli_id and args.meli_id not in meli_ids:
        meli_ids.append(args.meli_id)
    last_result = read_json(Path(args.result), {})
    last_payloads = read_json(Path(args.payload), [])
    last_payload = last_payloads[0] if isinstance(last_payloads, list) and last_payloads else {}
    blockers[shopify_id] = {
        "title": selected.get("title"),
        "blocked_at": time.strftime("%Y-%m-%d"),
        "reason": args.reason,
        "reason_type": args.reason_type,
        "meli_item_ids": meli_ids,
        "resolution_attempts": {
            "category": source.get("category_resolution") or {},
            "meli_validation": last_result.get("validation") or {},
            "title": {
                "title_change_reason": last_payload.get("title_change_reason"),
                "title_rejection_evidence": last_payload.get("title_rejection_evidence"),
            },
        },
        "do_not_auto_republish": True,
    }
    atomic_write_json(Path(args.blocked_products), blockers)
    reservations_path = Path(
        getattr(args, "reservations", RESERVATIONS_FILE)
    ).resolve()
    reservations = read_json(reservations_path, {})
    if shopify_id in reservations:
        reservations.pop(shopify_id)
        atomic_write_json(reservations_path, reservations)
    print(
        json.dumps(
            {
                "outcome": "blocked",
                "shopify_id": shopify_id,
                "meli_id": args.meli_id,
                "reason": args.reason,
            },
            ensure_ascii=False,
        )
    )
    return 0


def submit_payload(args: argparse.Namespace) -> int:
    # Import lazily so prepare/check-payload remain usable without loading the
    # publication implementation or touching Mercado Libre.
    from publish_payloads import run

    return run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and enforce the one-product Shopify -> Mercado Libre contract."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Select one product and fetch its Meli requirements.")
    prepare_parser.add_argument("--mode", choices=("dry-run", "publish"), default="dry-run")
    prepare_parser.add_argument("--limit", type=int, default=250)
    prepare_parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    prepare_parser.add_argument("--mappings", default=str(MAPPINGS_FILE))
    prepare_parser.add_argument("--blocked-products", default=str(BLOCKED_PRODUCTS_FILE))
    prepare_parser.add_argument("--reservations", default=str(RESERVATIONS_FILE))
    prepare_parser.add_argument("--journal", default=str(PUBLICATION_JOURNAL_FILE))
    prepare_parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    prepare_parser.set_defaults(handler=prepare)

    check_parser = subparsers.add_parser("check-payload", help="Validate one optimized payload locally.")
    check_parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    check_parser.add_argument("--payload", default=str(DEFAULT_WORK_DIR / "productos_listos.json"))
    check_parser.set_defaults(handler=check_payload)

    discover_parser = subparsers.add_parser(
        "discover-category",
        help="Add candidates using a source-supported functional product query.",
    )
    discover_parser.add_argument("--query", required=True)
    discover_parser.add_argument("--reason", required=True)
    discover_parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    discover_parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    discover_parser.set_defaults(handler=discover_categories)

    category_parser = subparsers.add_parser(
        "select-category",
        help="Select one discovered category and fetch its current attributes.",
    )
    category_parser.add_argument("--category-id", required=True)
    category_parser.add_argument("--reason", required=True)
    category_parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    category_parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    category_parser.set_defaults(handler=select_category)

    block_parser = subparsers.add_parser("block", help="Record a concrete terminal product blocker.")
    block_parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    block_parser.add_argument("--blocked-products", default=str(BLOCKED_PRODUCTS_FILE))
    block_parser.add_argument("--reason", required=True)
    block_parser.add_argument(
        "--reason-type",
        choices=("policy", "missing_data", "image", "validation", "moderation"),
        required=True,
    )
    block_parser.add_argument("--meli-id")
    block_parser.add_argument("--result", default=str(DEFAULT_WORK_DIR / "result.json"))
    block_parser.add_argument("--payload", default=str(DEFAULT_WORK_DIR / "productos_listos.json"))
    block_parser.add_argument("--reservations", default=str(RESERVATIONS_FILE))
    block_parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    block_parser.set_defaults(handler=block_product)

    submit_parser = subparsers.add_parser(
        "submit",
        help="Validate exactly one payload or publish it when --publish is explicit.",
    )
    submit_parser.add_argument("--publish", action="store_true")
    submit_parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    submit_parser.add_argument("--payload", default=str(DEFAULT_WORK_DIR / "productos_listos.json"))
    submit_parser.add_argument("--result", default=str(DEFAULT_WORK_DIR / "result.json"))
    submit_parser.add_argument("--mappings", default=str(MAPPINGS_FILE))
    submit_parser.add_argument("--quality-records", default=str(QUALITY_RECORDS_FILE))
    submit_parser.add_argument("--blocked-products", default=str(BLOCKED_PRODUCTS_FILE))
    submit_parser.add_argument("--journal", default=str(PUBLICATION_JOURNAL_FILE))
    submit_parser.add_argument("--reservations", default=str(RESERVATIONS_FILE))
    submit_parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    submit_parser.add_argument("--poll-attempts", type=int, default=8)
    submit_parser.add_argument("--poll-delay", type=float, default=10)
    submit_parser.add_argument("--stable-checks", type=int, default=2)
    submit_parser.set_defaults(handler=submit_payload)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command in {
            "prepare",
            "discover-category",
            "select-category",
            "block",
            "submit",
        }:
            with workflow_lock(Path(args.lock).resolve()):
                return int(args.handler(args))
        return int(args.handler(args))
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "failed_retryable",
                    "reason": str(error),
                    "command": args.command,
                },
                ensure_ascii=False,
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
