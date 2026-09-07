#!/usr/bin/env python3
"""Validate or publish exactly one agent-prepared Mercado Libre payload."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


REPO_ROOT = Path(__file__).resolve().parents[3]
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
    USER_PRODUCTS_FAMILY_MODE,
    WORKFLOW_LOCK_FILE,
    assert_prepared_payload,
    atomic_write_json,
    load_blocked_products,
    read_json,
    workflow_lock,
)
from shared.meli_client import (  # noqa: E402
    get_meli_access_token,
    get_meli_headers,
    publish_meli_item,
    update_meli_item_description,
)
from sync_products import build_meli_payload, validate_item_on_meli  # noqa: E402


CHARGER_GTIN_EXCEPTION_CATEGORIES = {"MLC157684", "MLC159239"}


def write_result(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(path, result)
    print(json.dumps(result, ensure_ascii=False))
    return result


def response_causes(response: requests.Response) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        causes = response.json().get("cause", [])
    except (ValueError, AttributeError):
        causes = []
    errors = [cause for cause in causes if cause.get("type") == "error"]
    warnings = [cause for cause in causes if cause.get("type") == "warning"]
    return errors, warnings


def validation_succeeded(response: requests.Response) -> bool:
    if response.status_code in (200, 204):
        return True
    if response.status_code != 400:
        return False
    errors, warnings = response_causes(response)
    return bool(warnings) and not errors


def validation_failure_is_retryable(validation: dict[str, Any]) -> bool:
    attempts = validation.get("attempts") or []
    if not attempts:
        return True
    status_code = attempts[-1].get("status_code")
    return status_code in {401, 403, 408, 409, 425, 429} or (
        isinstance(status_code, int) and status_code >= 500
    )


def requires_user_product_format(response: requests.Response) -> bool:
    errors, _ = response_causes(response)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return any(
        "family_name" in str(error.get("message", ""))
        or "family_name" in (error.get("references") or [])
        or ("title" in str(error.get("message", "")) and "invalid" in str(error.get("message", "")))
        for error in errors
    ) or (body.get("message") == "body.invalid_fields" and "title" in str(body.get("error", "")))


def replace_empty_gtin_with_category_exception(item: dict[str, Any]) -> dict[str, Any]:
    replacement = dict(item)
    replacement["attributes"] = [
        {"id": "GTIN", "value_name": "No aplica"}
        if attribute.get("id") == "EMPTY_GTIN_REASON"
        else attribute
        for attribute in item.get("attributes") or []
    ]
    return replacement


def has_missing_conditional_identifier(response: requests.Response) -> bool:
    errors, _ = response_causes(response)
    return any(
        error.get("code") == "item.attribute.missing_conditional_required"
        for error in errors
    )


def build_and_validate(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    # The listing owns the whole Shopify variant matrix.  A zero quantity is
    # valid and must remain visible as an unavailable Meli variation.
    publish_variations = [
        variation
        for variation in payload.get("variations") or []
        if variation.get("publication_disposition", "publish") == "publish"
    ]
    kwargs = {
        "optimized_data": payload["ai_data"],
        "category_id": payload["category_id"],
        "stock": payload["stock"],
        "price": payload["price"],
        "pictures": [{"source": url} for url in payload["images"]],
        "extra_attributes": payload.get("extra_attributes", []),
        "barcode": payload.get("barcode", ""),
        "shipping": payload.get("shipping"),
        "variations": publish_variations or None,
    }

    attempts: list[dict[str, Any]] = []
    item = build_meli_payload(use_catalog_format=False, **kwargs)
    response = validate_item_on_meli(item)
    errors, warnings = response_causes(response)
    attempts.append(
        {
            "format": "standard",
            "status_code": response.status_code,
            "errors": errors,
            "warnings": warnings,
        }
    )
    if validation_succeeded(response):
        return item, {"attempts": attempts, "warnings": warnings}

    if (
        payload["category_id"] in CHARGER_GTIN_EXCEPTION_CATEGORIES
        and has_missing_conditional_identifier(response)
        and any(attribute.get("id") == "EMPTY_GTIN_REASON" for attribute in item.get("attributes") or [])
    ):
        exceptional_item = replace_empty_gtin_with_category_exception(item)
        exceptional_response = validate_item_on_meli(exceptional_item)
        errors, warnings = response_causes(exceptional_response)
        attempts.append(
            {
                "format": "category_gtin_exception",
                "status_code": exceptional_response.status_code,
                "errors": errors,
                "warnings": warnings,
            }
        )
        if validation_succeeded(exceptional_response):
            return exceptional_item, {"attempts": attempts, "warnings": warnings}

    return None, {"attempts": attempts, "warnings": warnings}


def is_family_candidate(candidate: dict[str, Any]) -> bool:
    return candidate.get("_publication_mode") == USER_PRODUCTS_FAMILY_MODE


def _live_attribute_value(item: dict[str, Any], attribute_id: str) -> str:
    for attribute in item.get("attributes") or []:
        if attribute.get("id") == attribute_id:
            return str(attribute.get("value_name") or attribute.get("value_id") or "").strip().casefold()
    return ""


def verify_family_member(live: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    expected = spec["payload"]
    errors = []
    if not live_gate_passes(live):
        errors.append("item is not clean active")
    if int(live.get("available_quantity") or -1) != int(expected["available_quantity"]):
        errors.append("stock differs from Shopify variant")
    if int(float(live.get("price") or -1)) != int(float(expected["price"])):
        errors.append("price differs from Shopify variant")
    live_sku = str(live.get("seller_custom_field") or "").strip()
    if live_sku != spec["sku"] and _live_attribute_value(live, "SELLER_SKU") != spec["sku"].casefold():
        errors.append("SKU differs from Shopify variant")
    for combination in spec.get("attribute_combinations") or []:
        expected_value = str(combination.get("value_name") or combination.get("value_id") or "").strip().casefold()
        if _live_attribute_value(live, str(combination.get("id") or "")) != expected_value:
            errors.append(f"option {combination.get('id')} differs from Shopify variant")
    if not (live.get("pictures") or []):
        errors.append("variant image is absent")
    if not live.get("shipping"):
        errors.append("shipping is absent")
    return errors


def fetch_item(item_id: str) -> dict[str, Any] | None:
    response = requests.get(
        f"https://api.mercadolibre.com/items/{item_id}",
        headers=get_meli_headers(),
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def upload_original_pictures_to_meli(image_urls: list[str]) -> list[str]:
    upload_headers = {"Authorization": f"Bearer {get_meli_access_token()}"}
    picture_ids = []
    for index, image_url in enumerate(image_urls, start=1):
        image_response = requests.get(image_url, timeout=60)
        image_response.raise_for_status()
        content_type = image_response.headers.get("content-type") or "image/jpeg"
        extension = (
            mimetypes.guess_extension(content_type.split(";")[0])
            or os.path.splitext(urlparse(image_url).path)[1]
            or ".jpg"
        )
        upload_response = requests.post(
            "https://api.mercadolibre.com/pictures/items/upload",
            headers=upload_headers,
            files={"file": (f"shopify_image_{index}{extension}", image_response.content, content_type)},
            timeout=120,
        )
        upload_response.raise_for_status()
        picture_id = upload_response.json().get("id")
        if not picture_id:
            raise RuntimeError("Mercado Libre picture upload returned no id")
        picture_ids.append(picture_id)
    return picture_ids


def update_item(item_id: str, update: dict[str, Any]) -> None:
    response = requests.put(
        f"https://api.mercadolibre.com/items/{item_id}",
        headers=get_meli_headers(),
        json=update,
        timeout=60,
    )
    response.raise_for_status()


def live_gate_passes(item: dict[str, Any]) -> bool:
    return (
        item.get("status") == "active"
        and not (item.get("sub_status") or [])
        and not (item.get("warnings") or [])
    )


def verify_live_variations(live_item: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Ensure Meli retained every Shopify variant, including stock-zero ones."""
    expected = payload.get("variations") or []
    if not expected:
        return []
    actual = live_item.get("variations") or []
    actual_by_sku = {
        str(variation.get("seller_custom_field") or "").strip(): variation
        for variation in actual
        if str(variation.get("seller_custom_field") or "").strip()
    }
    errors: list[str] = []
    for variation in expected:
        sku = str(variation.get("sku") or "").strip()
        actual_variation = actual_by_sku.get(sku)
        if not actual_variation:
            errors.append(f"missing Shopify variation SKU {sku}")
            continue
        if int(actual_variation.get("available_quantity") or 0) != int(variation.get("stock") or 0):
            errors.append(f"variation {sku} stock differs from Shopify")
        if abs(float(actual_variation.get("price") or 0) - float(variation.get("price") or 0)) > 0.001:
            errors.append(f"variation {sku} price differs from Shopify")
    return errors


def ensure_item_active(
    item_id: str,
    image_urls: list[str],
    attempts: int = 8,
    delay_seconds: float = 10,
    stable_checks: int = 2,
) -> dict[str, Any]:
    """Require repeated clean live reads; never blindly reactivate moderation."""
    uploaded_pending_pictures = False
    clean_reads = 0
    last_item: dict[str, Any] = {}
    for attempt in range(attempts):
        item = fetch_item(item_id)
        if item is None:
            return {"id": item_id, "status": "deleted", "sub_status": ["deleted"], "warnings": []}
        last_item = item
        sub_status = item.get("sub_status") or []
        if live_gate_passes(item):
            clean_reads += 1
            if clean_reads >= stable_checks:
                return item
        else:
            clean_reads = 0

        if "picture_download_pending" in sub_status and not uploaded_pending_pictures:
            picture_ids = upload_original_pictures_to_meli(image_urls)
            update_item(item_id, {"pictures": [{"id": picture_id} for picture_id in picture_ids]})
            uploaded_pending_pictures = True

        if attempt + 1 < attempts and delay_seconds:
            time.sleep(delay_seconds)
    return last_item


def audit_persisted_attributes(item_id: str, expected_item: dict[str, Any]) -> dict[str, Any]:
    live_item = fetch_item(item_id) or {}
    expected_ids = {attribute.get("id") for attribute in expected_item.get("attributes") or []}
    live_ids = {attribute.get("id") for attribute in live_item.get("attributes") or []}
    missing = sorted(attribute_id for attribute_id in expected_ids - live_ids if attribute_id)
    patched = bool(missing)
    if patched:
        update_item(item_id, {"attributes": expected_item.get("attributes") or []})
        live_item = fetch_item(item_id) or {}
        live_ids = {attribute.get("id") for attribute in live_item.get("attributes") or []}
        missing = sorted(attribute_id for attribute_id in expected_ids - live_ids if attribute_id)
    live_item["_missing_expected_attributes"] = missing
    live_item["_attributes_patched"] = patched
    return live_item


def build_quality_record(
    payload: dict[str, Any], live_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Create a durable, API-evidence-based post-live quality record."""
    achieved = [
        "category_resolved",
        "source_faithful_title",
        "shopify_faithful_gallery",
        "supported_attributes_persisted",
        "clean_active_gate",
        "mapping_persisted",
    ]
    omitted = list(payload.get("quality_omissions") or [])
    if all((item.get("shipping") or {}).get("free_shipping") is True for item in live_items):
        achieved.append("free_shipping_live")
    else:
        omitted.append(
            {
                "objective": "free_shipping_live",
                "reason": "Mercado Libre did not persist free shipping in the live item",
            }
        )
    return {
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "achieved": achieved,
        "omitted": omitted,
        "visual_quality_score": {
            "available_via_api": False,
            "reason": "not exposed by the item API",
        },
        "live_item_ids": [str(item.get("id") or "") for item in live_items],
    }


def persist_quality_record(path: Path, shopify_id: str, record: dict[str, Any]) -> None:
    records = read_json(path, {})
    if not isinstance(records, dict):
        raise ValueError("publication quality records must be a JSON object")
    records[shopify_id] = record
    atomic_write_json(path, records)


def record_blocker(
    blocked_path: Path,
    shopify_id: str,
    title: str,
    meli_id: str | None,
    reason: str,
    final_item: dict[str, Any],
) -> None:
    blockers = load_blocked_products(blocked_path)
    previous = blockers.get(shopify_id) or {}
    item_ids = list(previous.get("meli_item_ids") or [])
    if meli_id and meli_id not in item_ids:
        item_ids.append(meli_id)
    blockers[shopify_id] = {
        "title": previous.get("title") or title,
        "blocked_at": time.strftime("%Y-%m-%d"),
        "reason": reason,
        "reason_type": "moderation",
        "meli_item_ids": item_ids,
        "last_status": final_item.get("status"),
        "last_sub_status": final_item.get("sub_status") or [],
        "last_warnings": final_item.get("warnings") or [],
        "do_not_auto_republish": True,
    }
    atomic_write_json(blocked_path, blockers)


def remove_journal_entry(journal_path: Path, shopify_id: str) -> None:
    journal = read_json(journal_path, {})
    if shopify_id in journal:
        journal.pop(shopify_id)
        atomic_write_json(journal_path, journal)


def remove_reservation(reservations_path: Path, shopify_id: str) -> None:
    reservations = read_json(reservations_path, {})
    if shopify_id in reservations:
        reservations.pop(shopify_id)
        atomic_write_json(reservations_path, reservations)


def publish_family_candidate(
    *,
    args: argparse.Namespace,
    candidate: dict[str, Any],
    payload: dict[str, Any],
    validation: dict[str, Any],
    shopify_id: str,
    title: str,
    mappings: dict[str, Any],
    mappings_path: Path,
    blocked_path: Path,
    journal_path: Path,
    reservations_path: Path,
    result_path: Path,
) -> int:
    """Create/resume a User Products family and map every Shopify variant."""
    existing_mapping = mappings.get(shopify_id)
    existing_variants = (
        dict(existing_mapping.get("variants") or {})
        if isinstance(existing_mapping, dict)
        and existing_mapping.get("mode") == USER_PRODUCTS_FAMILY_MODE
        else {}
    )
    candidate_items = [
        spec
        for spec in candidate["items"]
        if spec["shopify_variant_id"] not in existing_variants
    ]
    if not candidate_items:
        raise RuntimeError("family expansion has no unmapped sellable variant")
    journal = read_json(journal_path, {})
    entry = journal.get(shopify_id) or {
        "mode": USER_PRODUCTS_FAMILY_MODE,
        "family_name": candidate["family_name"],
        "stage": "prepared",
        "variants": {},
    }
    journal[shopify_id] = entry
    atomic_write_json(journal_path, journal)
    verified_live_items: list[dict[str, Any]] = []
    try:
        for spec in candidate_items:
            variant_id = spec["shopify_variant_id"]
            state = entry["variants"].get(variant_id) or {}
            item_id = str(state.get("meli_item_id") or "")
            adopt_item_id = str(spec.get("adopt_meli_item_id") or "")
            if not item_id and adopt_item_id:
                item_id = adopt_item_id
                entry["variants"][variant_id] = {
                    "meli_item_id": item_id,
                    "sku": spec["sku"],
                    "stage": "adopting",
                    "adopted": True,
                }
                atomic_write_json(journal_path, journal)
            if item_id:
                existing = fetch_item(item_id)
                if existing is None or existing.get("status") == "deleted" or "deleted" in (existing.get("sub_status") or []):
                    raise RuntimeError(
                        f"journaled family item {item_id} disappeared; automatic replacement is forbidden"
                    )
                if existing.get("status") == "paused" and state.get("stage") == "paused_after_failure":
                    update_item(item_id, {"status": "active"})
                    state["stage"] = "resuming"
                    atomic_write_json(journal_path, journal)
            else:
                creation = publish_meli_item(spec["payload"])
                item_id = str(creation.get("id") or "")
                if not item_id:
                    raise RuntimeError("Mercado Libre created no family item id")
                entry["variants"][variant_id] = {
                    "meli_item_id": item_id,
                    "sku": spec["sku"],
                    "stage": "created",
                }
                atomic_write_json(journal_path, journal)

            if not update_meli_item_description(item_id, payload["ai_data"]["clean_description"]):
                raise RuntimeError(f"description upload did not succeed for {item_id}")
            live = ensure_item_active(
                item_id,
                spec["images"],
                attempts=args.poll_attempts,
                delay_seconds=args.poll_delay,
                stable_checks=args.stable_checks,
            )
            if live_gate_passes(live):
                live = audit_persisted_attributes(item_id, spec["payload"])
            member_errors = verify_family_member(live, spec)
            missing_attributes = live.get("_missing_expected_attributes") or []
            if missing_attributes:
                member_errors.append(
                    "missing attributes: " + ", ".join(missing_attributes)
                )
            if member_errors:
                raise RuntimeError(
                    f"family verification failed for {item_id}: " + "; ".join(member_errors)
                )
            entry["variants"][variant_id].update(
                {
                    "stage": "verified",
                    "sku": spec["sku"],
                    "user_product_id": live.get("user_product_id"),
                    "family_id": live.get("family_id"),
                }
            )
            verified_live_items.append(live)
            atomic_write_json(journal_path, journal)

        states = entry["variants"]
        family_ids = {str(state.get("family_id") or "") for state in states.values()}
        existing_family_id = (
            str(existing_mapping.get("family_id") or "")
            if isinstance(existing_mapping, dict)
            else ""
        )
        if existing_family_id:
            family_ids.add(existing_family_id)
        user_product_ids = {
            str(state.get("user_product_id") or "") for state in states.values()
        }
        user_product_ids.update(
            str(value.get("user_product_id") or "")
            for value in existing_variants.values()
            if isinstance(value, dict)
        )
        if len(family_ids) != 1 or "" in family_ids:
            raise RuntimeError("Meli items do not share one non-empty family_id")
        if len(user_product_ids) != len(candidate_items) + len(existing_variants) or "" in user_product_ids:
            raise RuntimeError("Meli items do not have distinct user_product_id values")

        published_variant_ids = {
            *existing_variants,
            *(spec["shopify_variant_id"] for spec in candidate_items),
        }
        unpublished = {
            str(variation.get("shopify_variant_id")): {
                "sku": variation.get("sku") or None,
                "reason": (
                    variation.get("omit_reason")
                    if variation.get("publication_disposition") == "omit"
                    else "not_sellable"
                ),
            }
            for variation in payload.get("variations") or []
            if str(variation.get("shopify_variant_id")) not in published_variant_ids
        }
        family_id = next(iter(family_ids))
        mapping_variants = dict(existing_variants)
        for spec in candidate_items:
            state = states[spec["shopify_variant_id"]]
            mapping_variants[spec["shopify_variant_id"]] = {
                "shopify_variant_id": spec["shopify_variant_id"],
                "shopify_inventory_item_id": spec.get("shopify_inventory_item_id") or "",
                "sku": spec["sku"],
                "meli_item_id": state["meli_item_id"],
                "user_product_id": state["user_product_id"],
            }
        mapping = {
            "mode": USER_PRODUCTS_FAMILY_MODE,
            "family_id": family_id,
            "family_name": (
                existing_mapping.get("family_name")
                if isinstance(existing_mapping, dict)
                else candidate["family_name"]
            ),
            "variants": mapping_variants,
            "unpublished_variants": unpublished,
            "retired_meli_item_ids": [],
        }
        quality_record = build_quality_record(
            payload,
            verified_live_items,
        )
        persist_quality_record(
            Path(getattr(args, "quality_records", QUALITY_RECORDS_FILE)).resolve(),
            shopify_id,
            quality_record,
        )
        mappings[shopify_id] = mapping
        atomic_write_json(mappings_path, mappings)
        remove_journal_entry(journal_path, shopify_id)
        remove_reservation(reservations_path, shopify_id)
        write_result(
            result_path,
            {
                "published": True,
                "shopify_id": shopify_id,
                "meli_ids": [
                    states[spec["shopify_variant_id"]]["meli_item_id"]
                    for spec in candidate_items
                ],
                "expanded_existing_family": bool(existing_variants),
                "mode": "publish",
                "publication_mode": USER_PRODUCTS_FAMILY_MODE,
                "outcome": "published",
                "family_id": family_id,
                "mapping": mapping,
                "quality_record": quality_record,
                "validation": validation,
            },
        )
        return 0
    except Exception as error:
        # A partially-created family must not remain sellable. These are only
        # items created by this journal, so pausing is reversible and the next
        # authorized run can resume the same IDs without duplication.
        for state in entry.get("variants", {}).values():
            item_id = state.get("meli_item_id")
            if not item_id:
                continue
            if state.get("adopted"):
                continue
            try:
                live = fetch_item(str(item_id))
                if live and live.get("status") == "active":
                    update_item(str(item_id), {"status": "paused"})
                state["stage"] = "paused_after_failure"
            except Exception:
                pass
        atomic_write_json(journal_path, journal)
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "meli_ids": [
                    state.get("meli_item_id")
                    for state in entry.get("variants", {}).values()
                    if state.get("meli_item_id")
                ],
                "mode": "publish",
                "publication_mode": USER_PRODUCTS_FAMILY_MODE,
                "outcome": "failed_retryable",
                "reason": str(error),
                "resume_same_item": bool(entry.get("variants")),
            },
        )
        return 4


def run(args: argparse.Namespace) -> int:
    source_path = Path(args.source).resolve()
    payload_path = Path(args.payload).resolve()
    result_path = Path(args.result).resolve()
    mappings_path = Path(args.mappings).resolve()
    blocked_path = Path(args.blocked_products).resolve()
    journal_path = Path(args.journal).resolve()
    reservations_path = Path(
        getattr(args, "reservations", RESERVATIONS_FILE)
    ).resolve()
    mode = "publish" if args.publish else "dry-run"

    try:
        source = read_json(source_path, {})
        payload = assert_prepared_payload(read_json(payload_path, None), source)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        write_result(result_path, {"published": False, "mode": mode, "outcome": "invalid_payload", "reason": str(error)})
        return 2

    if args.publish and source.get("mode") != "publish":
        write_result(
            result_path,
            {
                "published": False,
                "mode": mode,
                "outcome": "invalid_payload",
                "reason": "live publication requires source.json prepared with --mode publish",
            },
        )
        return 2

    shopify_id = str(payload["shopify_id"])
    title = str(payload["ai_data"]["optimized_title"])
    mappings = read_json(mappings_path, {})
    blockers = load_blocked_products(blocked_path)
    if shopify_id in blockers and blockers[shopify_id].get("do_not_auto_republish", True):
        remove_reservation(reservations_path, shopify_id)
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "mode": mode,
                "outcome": "blocked",
                "reason": blockers[shopify_id].get("reason", "product is blocked"),
            },
        )
        return 3
    if shopify_id in mappings:
        remove_reservation(reservations_path, shopify_id)
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "meli_id": mappings[shopify_id],
                "mode": mode,
                "outcome": "already_synced",
            },
        )
        return 0

    try:
        candidate, validation = build_and_validate(payload)
    except Exception as error:
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "mode": mode,
                "outcome": "failed_retryable",
                "reason": str(error),
                "resume_same_item": False,
            },
        )
        return 4
    if candidate is None:
        outcome = (
            "failed_retryable"
            if validation_failure_is_retryable(validation)
            else "validation_failed"
        )
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "mode": mode,
                "outcome": outcome,
                "reason": "Mercado Libre rejected the prepared payload",
                "validation": validation,
            },
        )
        return 4 if outcome == "failed_retryable" else 2

    if not args.publish:
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "mode": "dry-run",
                "outcome": "validated",
                "publication_mode": "single_item",
                "validation": validation,
            },
        )
        return 0

    journal = read_json(journal_path, {})
    journal_entry = journal.get(shopify_id) or {}
    meli_id = journal_entry.get("meli_id")
    if not meli_id:
        adoption_ids = {
            str(decision.get("meli_item_id") or "")
            for decision in (payload.get("reconciliation") or {}).values()
            if decision.get("decision") == "adopt" and decision.get("meli_item_id")
        }
        if len(adoption_ids) == 1:
            meli_id = next(iter(adoption_ids))
            journal_entry = {
                "meli_id": meli_id,
                "stage": "adopting",
                "adopted": True,
            }
            journal[shopify_id] = journal_entry
            atomic_write_json(journal_path, journal)
    created_now = False
    try:
        if meli_id:
            existing = fetch_item(str(meli_id))
            if existing is None or existing.get("status") == "deleted" or "deleted" in (existing.get("sub_status") or []):
                reason = "Previously created item disappeared or was deleted before completion"
                record_blocker(blocked_path, shopify_id, title, str(meli_id), reason, existing or {})
                remove_reservation(reservations_path, shopify_id)
                write_result(
                    result_path,
                    {
                        "published": False,
                        "shopify_id": shopify_id,
                        "meli_id": meli_id,
                        "mode": "publish",
                        "outcome": "blocked",
                        "reason": reason,
                    },
                )
                return 3
        else:
            creation = publish_meli_item(candidate)
            meli_id = creation.get("id")
            if not meli_id:
                raise RuntimeError("Mercado Libre created no item id")
            created_now = True
            journal[shopify_id] = {
                "meli_id": meli_id,
                "stage": "created",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            atomic_write_json(journal_path, journal)

        if not update_meli_item_description(str(meli_id), payload["ai_data"]["clean_description"]):
            raise RuntimeError("description upload did not succeed")
        journal = read_json(journal_path, {})
        journal.setdefault(shopify_id, {})["stage"] = "description_uploaded"
        atomic_write_json(journal_path, journal)

        final_item = ensure_item_active(
            str(meli_id),
            payload["images"],
            attempts=args.poll_attempts,
            delay_seconds=args.poll_delay,
            stable_checks=args.stable_checks,
        )
        if live_gate_passes(final_item):
            final_item = audit_persisted_attributes(str(meli_id), candidate)
            if (
                final_item.get("_attributes_patched")
                and not (final_item.get("_missing_expected_attributes") or [])
            ):
                final_item = ensure_item_active(
                    str(meli_id),
                    payload["images"],
                    attempts=args.poll_attempts,
                    delay_seconds=args.poll_delay,
                    stable_checks=args.stable_checks,
                )
                final_item["_missing_expected_attributes"] = []
        missing_attributes = final_item.get("_missing_expected_attributes") or []
        variation_errors = verify_live_variations(final_item, payload)
        if not live_gate_passes(final_item) or missing_attributes or variation_errors:
            reason = (
                "Mercado Libre item failed the final active gate; "
                f"status={final_item.get('status')}, sub_status={final_item.get('sub_status') or []}, "
                f"warnings={final_item.get('warnings') or []}, missing_attributes={missing_attributes}, "
                f"variation_errors={variation_errors}"
            )
            record_blocker(blocked_path, shopify_id, title, str(meli_id), reason, final_item)
            remove_reservation(reservations_path, shopify_id)
            write_result(
                result_path,
                {
                    "published": False,
                    "shopify_id": shopify_id,
                    "meli_id": meli_id,
                    "mode": "publish",
                    "outcome": "blocked",
                    "reason": reason,
                    "verification": {
                        "status": final_item.get("status"),
                        "sub_status": final_item.get("sub_status") or [],
                        "warnings": final_item.get("warnings") or [],
                    },
                },
            )
            return 3

        quality_record = build_quality_record(payload, [final_item])
        persist_quality_record(
            Path(getattr(args, "quality_records", QUALITY_RECORDS_FILE)).resolve(),
            shopify_id,
            quality_record,
        )
        mappings[shopify_id] = meli_id
        atomic_write_json(mappings_path, mappings)
        remove_journal_entry(journal_path, shopify_id)
        remove_reservation(reservations_path, shopify_id)
        write_result(
            result_path,
            {
                "published": True,
                "shopify_id": shopify_id,
                "meli_id": meli_id,
                "mode": "publish",
                "outcome": "published",
                "created_now": created_now,
                "verification": {"status": "active", "sub_status": [], "warnings": []},
                "shipping": final_item.get("shipping") or {},
                "quality_record": quality_record,
            },
        )
        return 0
    except Exception as error:
        write_result(
            result_path,
            {
                "published": False,
                "shopify_id": shopify_id,
                "meli_id": meli_id,
                "mode": "publish",
                "outcome": "failed_retryable",
                "reason": str(error),
                "resume_same_item": bool(meli_id),
            },
        )
        return 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or publish exactly one prepared Mercado Libre payload."
    )
    parser.add_argument("--publish", action="store_true", help="Create or resume the live item; default is validation only.")
    parser.add_argument("--source", default=str(DEFAULT_WORK_DIR / "source.json"))
    parser.add_argument("--payload", default=str(DEFAULT_WORK_DIR / "productos_listos.json"))
    parser.add_argument("--result", default=str(DEFAULT_WORK_DIR / "result.json"))
    parser.add_argument("--mappings", default=str(MAPPINGS_FILE))
    parser.add_argument("--quality-records", default=str(QUALITY_RECORDS_FILE))
    parser.add_argument("--blocked-products", default=str(BLOCKED_PRODUCTS_FILE))
    parser.add_argument("--journal", default=str(PUBLICATION_JOURNAL_FILE))
    parser.add_argument("--reservations", default=str(RESERVATIONS_FILE))
    parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    parser.add_argument("--poll-attempts", type=int, default=8)
    parser.add_argument("--poll-delay", type=float, default=10)
    parser.add_argument("--stable-checks", type=int, default=2)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with workflow_lock(Path(args.lock).resolve()):
        return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
