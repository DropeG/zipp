"""Pure contract helpers for the one-product Shopify -> Mercado Libre flow.

This module contains no API calls.  Keeping selection, payload validation and
state-file handling here makes the production scripts testable without Shopify
or Mercado Libre credentials.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
AUTOMATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WORK_DIR = REPO_ROOT / "data" / "product-publishing" / "one-by-one"
MAPPINGS_FILE = AUTOMATION_DIR / "sync_mappings.json"
BLOCKED_PRODUCTS_FILE = AUTOMATION_DIR / "sync_blocked_products.json"
LEGACY_BLOCKED_PRODUCTS_FILE = REPO_ROOT / "sync_blocked_products.json"
PUBLICATION_JOURNAL_FILE = AUTOMATION_DIR / "publication_journal.json"
RESERVATIONS_FILE = AUTOMATION_DIR / "selection_reservations.json"
QUALITY_RECORDS_FILE = AUTOMATION_DIR / "publication_quality_records.json"
WORKFLOW_LOCK_FILE = AUTOMATION_DIR / ".one_by_one.lock"

SCHEMA_VERSION = 2
USER_PRODUCTS_FAMILY_MODE = "user_products_family"

ZIPP_PATTERN = re.compile(r"\b(?:zipp\.cl|zipp\s+chile|zipp)\b", re.IGNORECASE)

CONTACT_PATTERNS = (
    re.compile(r"https?://|www\.", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:whats?app|wsp|instagram|facebook|tiktok|contacto)\b", re.IGNORECASE),
    re.compile(r"\b[a-z0-9-]+\.(?:cl|com|net|org|io)\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+?56\s*)?(?:\d[\s().-]*){8,9}(?!\d)"),
)

INTERNAL_DESCRIPTION_PATTERNS = (
    re.compile(r"\bshopify\b", re.IGNORECASE),
    re.compile(r"\bsku\b", re.IGNORECASE),
    re.compile(r"\bvariant[_ ]?id\b", re.IGNORECASE),
    re.compile(r"\bsync_mappings\b", re.IGNORECASE),
    re.compile(r"\bapi (?:error|validation|fallback)\b", re.IGNORECASE),
)


class ContractError(ValueError):
    """Raised when a prepared payload violates a non-negotiable rule."""


@contextmanager
def workflow_lock(path: Path):
    """Reject overlapping workflow commands instead of racing state files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "another one-by-one workflow command is already running"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON without leaving a partially-written state file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_blocked_products(
    canonical_path: Path = BLOCKED_PRODUCTS_FILE,
    legacy_path: Path = LEGACY_BLOCKED_PRODUCTS_FILE,
) -> dict[str, Any]:
    """Merge legacy and canonical blocker state without discarding history."""
    legacy = read_json(legacy_path, {})
    canonical = read_json(canonical_path, {})
    if not isinstance(legacy, dict) or not isinstance(canonical, dict):
        raise ContractError("Blocked-products state must be a JSON object")
    return {**legacy, **canonical}


def mapping_item_ids(mapping: Any) -> list[str]:
    """Return every Mercado Libre item ID represented by a mapping value.

    Legacy mappings are scalar ``Shopify product -> Meli item`` strings.  New
    User Products families are objects with one item per Shopify variant.
    Keeping this decoder in one place lets the old state remain valid while
    reconciliation can safely inspect every member of a family.
    """
    if isinstance(mapping, str):
        return [mapping]
    if not isinstance(mapping, dict):
        return []
    item_ids: list[str] = []
    direct = mapping.get("meli_item_id")
    if direct:
        item_ids.append(str(direct))
    for variant in (mapping.get("variants") or {}).values():
        if isinstance(variant, dict) and variant.get("meli_item_id"):
            item_ids.append(str(variant["meli_item_id"]))
    return list(dict.fromkeys(item_ids))


def mapping_variant_by_sku(mappings: dict[str, Any], sku: str) -> dict[str, Any] | None:
    """Find an explicit User Products variant mapping by its Shopify SKU."""
    expected = str(sku or "").strip()
    if not expected:
        return None
    for shopify_id, mapping in mappings.items():
        if not isinstance(mapping, dict) or mapping.get("mode") != USER_PRODUCTS_FAMILY_MODE:
            continue
        for shopify_variant_id, variant in (mapping.get("variants") or {}).items():
            if not isinstance(variant, dict) or str(variant.get("sku") or "").strip() != expected:
                continue
            if not variant.get("meli_item_id"):
                continue
            return {
                "shopify_id": str(shopify_id),
                "shopify_variant_id": str(shopify_variant_id),
                **variant,
            }
    return None


def variant_stock(variant: dict[str, Any]) -> int:
    try:
        return max(0, int(variant.get("inventory_quantity") or 0))
    except (TypeError, ValueError):
        return 0


def variant_price(variant: dict[str, Any]) -> float:
    try:
        return float(variant.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def sellable_variants(product: dict[str, Any]) -> list[dict[str, Any]]:
    """A variant is sellable only when that same variant has stock and price."""
    return [
        variant
        for variant in product.get("variants") or []
        if variant_stock(variant) > 0 and variant_price(variant) > 0
    ]


def missing_mapped_sellable_variant_ids(
    product: dict[str, Any], mapping: Any
) -> set[str]:
    """Return sellable Shopify variants absent from a structured family mapping."""
    if not isinstance(mapping, dict) or mapping.get("mode") != USER_PRODUCTS_FAMILY_MODE:
        return set()
    mapped_ids = {str(value) for value in (mapping.get("variants") or {})}
    return {
        str(variant.get("id") or "")
        for variant in sellable_variants(product)
        if str(variant.get("id") or "") not in mapped_ids
    }


def eligibility_reason(
    product: dict[str, Any],
    mappings: dict[str, Any],
    blocked_products: dict[str, Any],
) -> str | None:
    shopify_id = str(product.get("id") or "").strip()
    if not shopify_id:
        return "missing_shopify_id"
    # A Shopify product maps to one Meli item.  Its complete variant set,
    # including zero-stock options, is created together and stock maintenance
    # is handled by the stock-sync workflow afterwards.
    if shopify_id in mappings:
        return "already_synced"
    blocker = blocked_products.get(shopify_id)
    if blocker and blocker.get("do_not_auto_republish", True):
        return "blocked"
    if str(product.get("status") or "active").lower() != "active":
        return "not_active"
    if not product.get("variants"):
        return "no_variants"
    if not sellable_variants(product):
        return "no_sellable_variant"
    if not [image for image in product.get("images") or [] if image.get("src")]:
        return "no_images"
    return None


def select_first_eligible(
    products: Iterable[dict[str, Any]],
    mappings: dict[str, Any],
    blocked_products: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    skipped: list[dict[str, Any]] = []
    for product in products:
        reason = eligibility_reason(product, mappings, blocked_products)
        if reason is None:
            return product, skipped
        skipped.append(
            {
                "shopify_id": product.get("id"),
                "title": product.get("title"),
                "reason": reason,
            }
        )
    return None, skipped


def contains_contact_data(value: Any) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in CONTACT_PATTERNS)


def contains_zipp_trace(value: Any) -> bool:
    return bool(ZIPP_PATTERN.search(str(value or "")))


def sanitize_meli_title(shopify_title: str) -> str:
    """Remove only forbidden store-brand trace and normalize resulting spaces."""
    cleaned = ZIPP_PATTERN.sub("", str(shopify_title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,;:.)])", r"\1", cleaned)
    cleaned = re.sub(r"([(])\s+", r"\1", cleaned)
    return cleaned.strip(" -–—|,;")


def _buyer_facing_values(payload: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    ai_data = payload.get("ai_data") or {}
    yield "title", ai_data.get("optimized_title")
    yield "description", ai_data.get("clean_description")
    yield "brand", ai_data.get("brand")
    yield "model", ai_data.get("model")
    for attribute in payload.get("extra_attributes") or []:
        yield f"attribute:{attribute.get('id')}", attribute.get("value_name")


def _image_urls(product: dict[str, Any]) -> set[str]:
    return {
        str(image.get("src"))
        for image in product.get("images") or []
        if image.get("src")
    }


def _required_attribute_ids(source: dict[str, Any]) -> set[str]:
    required: set[str] = set()
    for attribute in source.get("category_attributes") or []:
        tags = attribute.get("tags") or {}
        tag_names = (
            {name for name, enabled in tags.items() if enabled}
            if isinstance(tags, dict)
            else set(tags)
        )
        if (
            "read_only" not in tag_names
            and tag_names.intersection({"required", "catalog_required", "required_for_catalog"})
        ):
            required.add(str(attribute.get("id")))
    return required


def _prepared_attribute_value(payload: dict[str, Any], attribute_id: str) -> str:
    for attribute in payload.get("extra_attributes") or []:
        if attribute.get("id") == attribute_id:
            return str(attribute.get("value_name") or "").strip()
    return ""


def _normalized_fact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def validate_prepared_payload(
    payloads: Any,
    source: dict[str, Any],
) -> list[str]:
    """Return every local contract violation before any Mercado Libre write."""
    errors: list[str] = []
    if not isinstance(payloads, list) or len(payloads) != 1:
        return ["productos_listos must be a JSON array containing exactly one product"]

    payload = payloads[0]
    selected = source.get("selected_product")
    if not isinstance(selected, dict):
        return ["source file does not contain one selected_product"]

    shopify_id = str(payload.get("shopify_id") or "")
    selected_id = str(selected.get("id") or "")
    if shopify_id != selected_id:
        errors.append("payload shopify_id does not match the selected product")
    source_category_id = str((source.get("category") or {}).get("id") or "")
    if str(payload.get("category_id") or "") != source_category_id:
        errors.append("payload category_id must equal the prepared Mercado Libre category")

    original_title = str(selected.get("title") or "").strip()
    payload_original_title = str(payload.get("original_title") or "").strip()
    optimized_title = str((payload.get("ai_data") or {}).get("optimized_title") or "").strip()
    if payload_original_title != original_title:
        errors.append("original_title must equal the Shopify title")
    initial_meli_title = sanitize_meli_title(original_title)
    if optimized_title != initial_meli_title:
        reason = payload.get("title_change_reason")
        if reason not in {"meli_title_limit", "contact_policy", "meli_rejection"}:
            errors.append(
                "optimized_title may differ from the sanitized Shopify title only after a documented Meli rejection"
            )
        rejection = payload.get("title_rejection_evidence")
        if not isinstance(rejection, dict) or not str(
            rejection.get("message") or rejection.get("code") or ""
        ).strip():
            errors.append("alternative title requires title_rejection_evidence from Meli")
    elif initial_meli_title != original_title and payload.get("title_change_reason") != "store_brand_policy":
        errors.append("removing Zipp from the Shopify title requires store_brand_policy")

    description = str((payload.get("ai_data") or {}).get("clean_description") or "")
    if "Características del producto:" not in description:
        errors.append("clean_description must include 'Características del producto:'")
    for pattern in INTERNAL_DESCRIPTION_PATTERNS:
        if pattern.search(description):
            errors.append("clean_description contains internal workflow information")
            break

    expected_brand = str(selected.get("meli_brand") or "Genérica").strip()
    prepared_brand = str((payload.get("ai_data") or {}).get("brand") or "").strip()
    if expected_brand.casefold() != "genérica" and prepared_brand != expected_brand:
        errors.append(
            "ai_data.brand must equal the explicit non-store Shopify meli_brand"
        )
    if expected_brand.casefold() == "genérica" and prepared_brand.casefold() != "genérica":
        evidence = payload.get("brand_evidence")
        if not isinstance(evidence, dict) or evidence.get("source") not in {
            "shopify_description",
            "shopify_image",
            "shopify_title_explicit_brand",
        } or not str(evidence.get("quote") or "").strip():
            errors.append(
                "non-generic brand requires evidence that it appears on the sold Shopify product"
            )
        else:
            quote = str(evidence.get("quote") or "").strip()
            evidence_source = evidence.get("source")
            if _normalized_fact(prepared_brand) not in _normalized_fact(quote):
                errors.append("brand_evidence quote does not contain the proposed brand")
            if evidence_source == "shopify_description" and quote.casefold() not in str(selected.get("body_html") or "").casefold():
                errors.append("brand_evidence quote is absent from the Shopify description")
            elif evidence_source == "shopify_title_explicit_brand" and quote.casefold() not in original_title.casefold():
                errors.append("brand_evidence quote is absent from the Shopify title")
            elif evidence_source == "shopify_image" and str(evidence.get("image_url") or "") not in _image_urls(selected):
                errors.append("brand_evidence image is not an original Shopify image")
    if contains_zipp_trace(prepared_brand):
        errors.append("Meli brand cannot contain Zipp store trace")

    model = str((payload.get("ai_data") or {}).get("model") or "").strip()
    if model.casefold() not in {"no aplica", "n/a", "na"}:
        evidence = payload.get("model_evidence")
        if not isinstance(evidence, dict) or evidence.get("source") not in {
            "shopify_description",
            "shopify_image",
            "shopify_title_explicit_model",
        } or not str(evidence.get("quote") or "").strip():
            errors.append(
                "non-'No aplica' model requires explicit manufacturer model evidence from Shopify"
            )
        else:
            quote = str(evidence.get("quote") or "").strip()
            evidence_source = evidence.get("source")
            if _normalized_fact(model) not in _normalized_fact(quote):
                errors.append("model_evidence quote does not contain the proposed model")
            if evidence_source == "shopify_description" and quote.casefold() not in str(selected.get("body_html") or "").casefold():
                errors.append("model_evidence quote is absent from the Shopify description")
            elif evidence_source == "shopify_title_explicit_model" and quote.casefold() not in original_title.casefold():
                errors.append("model_evidence quote is absent from the Shopify title")
            elif evidence_source == "shopify_image" and str(evidence.get("image_url") or "") not in _image_urls(selected):
                errors.append("model_evidence image is not an original Shopify image")

        input_connector = _prepared_attribute_value(payload, "INPUT_CONNECTOR")
        output_connector = _prepared_attribute_value(payload, "OUTPUT_CONNECTOR")
        connector_pair = _normalized_fact(f"{input_connector} a {output_connector}")
        if input_connector and output_connector and _normalized_fact(model) == connector_pair:
            errors.append("connector pair cannot be used as the product model")

    for field, value in _buyer_facing_values(payload):
        if contains_contact_data(value):
            errors.append(f"buyer-facing field {field} contains contact data")
        if contains_zipp_trace(value):
            errors.append(f"buyer-facing field {field} contains forbidden Zipp store trace")

    allowed_images = _image_urls(selected)
    remediated_images: set[str] = set()
    for provenance in payload.get("image_provenance") or []:
        if not isinstance(provenance, dict):
            errors.append("image_provenance entries must be objects")
            continue
        source_url = str(provenance.get("source_url") or "")
        output_url = str(provenance.get("output_url") or "")
        if (
            provenance.get("transformation") != "background_removal"
            or source_url not in allowed_images
            or not output_url
            or not provenance.get("source_checksum")
            or not provenance.get("output_checksum")
            or not provenance.get("meli_rejection")
        ):
            errors.append(
                "background remediation requires Shopify source, checksums and actual Meli rejection"
            )
            continue
        remediated_images.add(output_url)
    payload_images = payload.get("images") or []
    if not payload_images:
        errors.append("payload must include at least one Shopify image")
    unknown_images = [
        url
        for url in payload_images
        if str(url) not in allowed_images | remediated_images
    ]
    if unknown_images:
        errors.append("payload images must be Shopify originals or audited background remediations")
    for provenance in payload.get("image_provenance") or []:
        if isinstance(provenance, dict) and provenance.get("output_url") in remediated_images:
            if provenance.get("source_url") not in payload_images:
                errors.append("remediated cover must retain its Shopify original in the gallery")

    shipping = payload.get("shipping") or {}
    if shipping.get("free_shipping") is not True:
        errors.append("shipping.free_shipping must default to true")
    if shipping.get("mode") not in {"me2", "not_specified"}:
        errors.append("shipping.mode must be me2 or not_specified")
    if shipping.get("local_pick_up") is not True:
        errors.append("shipping.local_pick_up must default to true")

    quality_omissions = payload.get("quality_omissions") or []
    if not isinstance(quality_omissions, list):
        errors.append("quality_omissions must be a list")
        quality_omissions = []
    for omission in quality_omissions:
        if not isinstance(omission, dict) or not str(omission.get("objective") or "").strip() or not str(omission.get("reason") or "").strip():
            errors.append("each quality_omission requires objective and reason")

    reconciliation = payload.get("reconciliation") or {}
    for variant_id, evidence in (
        (source.get("sku_reconciliation") or {}).get("variants") or {}
    ).items():
        matches = evidence.get("matches") or []
        if not matches:
            continue
        decision = reconciliation.get(str(variant_id)) or {}
        if len(matches) != 1:
            errors.append(
                f"Shopify variant {variant_id} has ambiguous existing Meli SKU matches"
            )
            continue
        expected_item_id = str(matches[0].get("meli_item_id") or "")
        if (
            decision.get("decision") != "adopt"
            or str(decision.get("meli_item_id") or "") != expected_item_id
        ):
            errors.append(
                f"Shopify variant {variant_id} requires explicit adoption of its unique exact-SKU item or a blocker"
            )

    selected_variants = selected.get("variants") or []
    prepared_variants = payload.get("variations")
    if prepared_variants:
        by_id = {
            str(variation.get("shopify_variant_id") or ""): variation
            for variation in prepared_variants
        }
        expected_ids = {str(variant.get("id") or "") for variant in selected_variants}
        if set(by_id) != expected_ids or len(prepared_variants) != len(by_id):
            errors.append("variations must include every Shopify variant exactly once")
        for variant in selected_variants:
            variant_id = str(variant.get("id") or "")
            prepared = by_id.get(variant_id)
            if not prepared:
                continue
            try:
                prepared_stock = int(prepared.get("stock") or 0)
            except (TypeError, ValueError):
                prepared_stock = -1
            if prepared_stock != variant_stock(variant):
                errors.append(f"variation {variant_id} stock differs from Shopify")
            try:
                prepared_price = float(prepared.get("price") or 0)
            except (TypeError, ValueError):
                prepared_price = -1
            if abs(prepared_price - variant_price(variant)) > 0.001:
                errors.append(f"variation {variant_id} price differs from Shopify")
            source_barcode = str(variant.get("barcode") or "").strip()
            if (
                source_barcode
                and str(prepared.get("barcode") or "").strip() != source_barcode
            ):
                errors.append(f"variation {variant_id} barcode differs from Shopify")
            variation_images = prepared.get("images") or []
            if any(
                str(url) not in allowed_images | remediated_images
                for url in variation_images
            ):
                errors.append(f"variation {variant_id} uses an unaudited image")
    elif len(selected_variants) > 1:
        errors.append("products with multiple Shopify variants must include variations")

    # Variants are a faithful representation of the Shopify product, not a
    # publication queue.  Therefore the same checks apply even at stock zero.
    publishable_variant_count = 0
    for variation in prepared_variants or []:
        variant_id = str(variation.get("shopify_variant_id") or "")
        disposition = str(variation.get("publication_disposition") or "publish")
        if disposition == "omit":
            errors.append(f"Shopify variation {variant_id} cannot be omitted from the Meli item")
            continue
        if disposition != "publish":
            errors.append(f"Shopify variation {variant_id} has invalid publication_disposition")
            continue
        publishable_variant_count += 1
        if not str(variation.get("sku") or "").strip():
            errors.append(f"Shopify variation {variant_id} requires a Shopify SKU")
        if len(selected_variants) > 1 and not (variation.get("images") or []):
            errors.append(f"Shopify variation {variant_id} requires an assigned Shopify image")
        if len(selected_variants) > 1 and not (variation.get("attribute_combinations") or []):
            errors.append(
                f"Shopify variation {variant_id} requires an unambiguous Meli option combination"
            )
    if prepared_variants and publishable_variant_count == 0:
        errors.append("at least one Shopify variant must remain publishable")

    maximum_stock = sum(variant_stock(variant) for variant in selected_variants)
    try:
        payload_stock = int(payload.get("stock") or 0)
    except (TypeError, ValueError):
        payload_stock = -1
    if payload_stock < 0 or payload_stock > maximum_stock:
        errors.append("top-level stock cannot exceed Shopify inventory")
    try:
        payload_price = float(payload.get("price") or 0)
        if payload_price <= 0:
            errors.append("top-level price must be greater than zero")
        source_prices = {variant_price(variant) for variant in sellable_variants(selected)}
        if payload_price > 0 and not any(
            abs(payload_price - source_price) <= 0.001
            for source_price in source_prices
        ):
            errors.append("top-level price must equal a sellable Shopify variant price")
    except (TypeError, ValueError):
        errors.append("top-level price must be numeric")

    provided_attribute_ids = {
        str(attribute.get("id")) for attribute in payload.get("extra_attributes") or []
    }
    for variation in prepared_variants or []:
        provided_attribute_ids.update(
            str(combination.get("id"))
            for combination in variation.get("attribute_combinations") or []
            if combination.get("id")
        )
    provided_attribute_ids.update({"BRAND", "MODEL"})
    if payload.get("barcode"):
        provided_attribute_ids.update({"GTIN", "EMPTY_GTIN_REASON"})
    missing_required = sorted(
        attribute_id
        for attribute_id in _required_attribute_ids(source)
        if attribute_id not in provided_attribute_ids
    )
    if missing_required:
        errors.append(
            "missing required category attributes: " + ", ".join(missing_required)
        )

    return errors


def assert_prepared_payload(payloads: Any, source: dict[str, Any]) -> dict[str, Any]:
    errors = validate_prepared_payload(payloads, source)
    if errors:
        raise ContractError("; ".join(errors))
    return payloads[0]
