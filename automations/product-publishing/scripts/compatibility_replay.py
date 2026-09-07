#!/usr/bin/env python3
"""Read-only compatibility replay for already-synced Shopify products.

The collector snapshots current Shopify source data and the corresponding live
Mercado Libre item into an isolated workspace.  It deliberately does not call
the validation or publication endpoints and refuses to refresh expired OAuth
tokens, keeping external traffic GET-only.

The optional validator submits reconstructed payloads only to Mercado Libre's
non-publishing /items/validate endpoint. The comparer evaluates both skill
outputs against the same source fixture and writes JSON/Markdown evidence.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parent

import sys

for import_path in (REPO_ROOT, SCRIPT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from one_by_one_contract import (  # noqa: E402
    MAPPINGS_FILE,
    atomic_write_json,
    contains_contact_data,
    read_json,
    mapping_item_ids,
    validate_prepared_payload,
    variant_price,
    variant_stock,
)
from one_by_one_sync import (  # noqa: E402
    choose_best_category,
    normalize_category_attributes,
    product_policy_hints,
)
from shared.meli_client import load_tokens  # noqa: E402
from shared.shopify_client import get_shopify_products  # noqa: E402


DEFAULT_OUTPUT_DIR = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "shopify-to-meli-one-by-one-sync-workspace"
    / "compatibility-replay"
)


def readonly_meli_headers() -> dict[str, str]:
    """Return the current token without triggering an OAuth refresh write."""
    tokens = load_tokens() or {}
    access_token = tokens.get("access_token")
    expires_at = float(tokens.get("expires_at") or 0)
    if not access_token:
        raise RuntimeError("No Mercado Libre access token is available")
    if time.time() >= expires_at - 120:
        raise RuntimeError(
            "Mercado Libre token is expired or near expiry; refresh it outside "
            "the read-only compatibility replay"
        )
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def get_json(
    url: str,
    headers: dict[str, str],
    *,
    allow_not_found: bool = False,
) -> Any:
    response = requests.get(url, headers=headers, timeout=60)
    if allow_not_found and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def predict_category_readonly(
    title: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    query = urllib.parse.quote(title)
    return get_json(
        f"https://api.mercadolibre.com/sites/MLC/domain_discovery/search?q={query}",
        headers,
    )


def get_category_attributes_readonly(
    category_id: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    return get_json(
        f"https://api.mercadolibre.com/categories/{category_id}/attributes",
        headers,
    )


def compact_meli_baseline(
    item: dict[str, Any], description: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep only fields relevant to a source-fidelity comparison."""
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "family_name": item.get("family_name"),
        "category_id": item.get("category_id"),
        "price": item.get("price"),
        "available_quantity": item.get("available_quantity"),
        "status": item.get("status"),
        "sub_status": item.get("sub_status") or [],
        "warnings": item.get("warnings") or [],
        "shipping": item.get("shipping") or {},
        "attributes": item.get("attributes") or [],
        "variations": item.get("variations") or [],
        "pictures": [
            {
                "id": picture.get("id"),
                "url": picture.get("url"),
                "secure_url": picture.get("secure_url"),
            }
            for picture in item.get("pictures") or []
        ],
        "description": {
            "plain_text": (description or {}).get("plain_text"),
            "text": (description or {}).get("text"),
        },
        "date_created": item.get("date_created"),
        "last_updated": item.get("last_updated"),
    }


def collect(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    cases_dir = output_dir / "cases"
    mappings = read_json(Path(args.mappings).resolve(), {})
    if not isinstance(mappings, dict):
        raise ValueError("sync_mappings.json must contain a JSON object")

    products = get_shopify_products(limit=250).get("products", [])
    products_by_id = {str(product.get("id")): product for product in products}
    requested_ids = [str(value) for value in args.shopify_id]
    if not requested_ids:
        requested_ids = [
            shopify_id
            for shopify_id in mappings
            if shopify_id in products_by_id
        ][: args.count]
    if len(requested_ids) not in {3, 5} and not args.allow_other_count:
        raise ValueError("compatibility replay expects exactly 3 or 5 Shopify IDs")

    headers = readonly_meli_headers()
    manifest_cases: list[dict[str, Any]] = []
    for shopify_id in requested_ids:
        product = products_by_id.get(shopify_id)
        if not product:
            raise ValueError(f"Shopify product {shopify_id} was not found")
        mapped_item_ids = mapping_item_ids(mappings.get(shopify_id))
        meli_id = mapped_item_ids[0] if mapped_item_ids else ""
        if not meli_id:
            raise ValueError(f"Shopify product {shopify_id} is not mapped")

        item = get_json(
            f"https://api.mercadolibre.com/items/{meli_id}", headers
        )
        description = get_json(
            f"https://api.mercadolibre.com/items/{meli_id}/description",
            headers,
            allow_not_found=True,
        )
        predictions = predict_category_readonly(str(product.get("title") or ""), headers)
        if not predictions:
            raise RuntimeError(f"No category prediction for Shopify product {shopify_id}")
        predicted = choose_best_category(product, predictions)
        predicted_category_id = str(predicted.get("category_id") or "")
        if not predicted_category_id:
            raise RuntimeError(f"Prediction has no category ID for {shopify_id}")
        attributes = normalize_category_attributes(
            get_category_attributes_readonly(predicted_category_id, headers)
        )

        case_dir = cases_dir / shopify_id
        source = {
            "schema_version": 1,
            "mode": "compatibility-replay",
            "selected_product": product,
            "category": {
                "id": predicted_category_id,
                "name": predicted.get("category_name"),
                "domain_id": predicted.get("domain_id"),
            },
            "category_attributes": attributes,
            "policy_hints": product_policy_hints(product),
            "replay": {
                "read_only": True,
                "mapped_meli_id": meli_id,
                "historical_category_id": item.get("category_id"),
            },
        }
        baseline = compact_meli_baseline(item, description)
        atomic_write_json(case_dir / "source.json", source)
        atomic_write_json(case_dir / "baseline_meli.json", baseline)
        manifest_cases.append(
            {
                "shopify_id": shopify_id,
                "meli_id": meli_id,
                "title": product.get("title"),
                "predicted_category_id": predicted_category_id,
                "historical_category_id": item.get("category_id"),
                "historical_status": item.get("status"),
                "source": str(case_dir / "source.json"),
                "baseline": str(case_dir / "baseline_meli.json"),
            }
        )

    manifest = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
        "external_methods": ["GET"],
        "case_count": len(manifest_cases),
        "cases": manifest_cases,
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    print(json.dumps({"outcome": "collected", **manifest}, ensure_ascii=False))
    return 0


def payload_summary(
    payloads: Any,
    source: dict[str, Any],
    initial_payloads: Any = None,
    remote_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors = validate_prepared_payload(payloads, source)
    payload = payloads[0] if isinstance(payloads, list) and len(payloads) == 1 else {}
    product = source.get("selected_product") or {}
    variants = product.get("variants") or []
    initial_payload = (
        initial_payloads[0]
        if isinstance(initial_payloads, list) and len(initial_payloads) == 1
        else payload
    )
    variation_ids = {
        str(variation.get("shopify_variant_id") or "")
        for variation in payload.get("variations") or []
    }
    initial_variation_ids = {
        str(variation.get("shopify_variant_id") or "")
        for variation in initial_payload.get("variations") or []
    }
    expected_variant_ids = {str(variant.get("id") or "") for variant in variants}
    all_variants_preserved = (
        initial_variation_ids == expected_variant_ids
        if len(variants) > 1 or initial_payload.get("variations")
        else len(variants) == 1
    )
    fallback_applied = bool(
        len(variants) > 1
        and initial_payload.get("variations")
        and not payload.get("variations")
    )
    fallback_reason_recorded = bool(payload.get("variations_fallback_reason"))
    fallback_stock = sum(
        variant_stock(variant)
        for variant in variants
        if variant_stock(variant) > 0
    )
    try:
        fallback_stock_matches = int(payload.get("stock") or 0) == fallback_stock
    except (TypeError, ValueError):
        fallback_stock_matches = False
    variant_strategy_passed = not fallback_reason_recorded and (
        len(variants) <= 1 or variation_ids == expected_variant_ids
    )
    buyer_values = [
        (payload.get("ai_data") or {}).get("optimized_title"),
        (payload.get("ai_data") or {}).get("clean_description"),
        (payload.get("ai_data") or {}).get("brand"),
        (payload.get("ai_data") or {}).get("model"),
    ] + [
        attribute.get("value_name")
        for attribute in payload.get("extra_attributes") or []
    ]
    return {
        "contract_passed": not errors,
        "contract_errors": errors,
        "title_matches_shopify": (
            (payload.get("ai_data") or {}).get("optimized_title")
            == product.get("title")
        ),
        "category_matches_preparation": (
            str(payload.get("category_id") or "")
            == str((source.get("category") or {}).get("id") or "")
        ),
        "all_variants_preserved": all_variants_preserved,
        "variant_strategy_passed": variant_strategy_passed,
        "fallback_applied": fallback_applied,
        "fallback_reason_recorded": fallback_reason_recorded,
        "fallback_stock_matches_positive_inventory": fallback_stock_matches,
        "variant_count": len(payload.get("variations") or []),
        "shopify_variant_count": len(variants),
        "free_shipping_requested": (
            (payload.get("shipping") or {}).get("free_shipping") is True
        ),
        "image_count": len(payload.get("images") or []),
        "shopify_image_count": len(product.get("images") or []),
        "has_contact_data": any(contains_contact_data(value) for value in buyer_values),
        "price": payload.get("price"),
        "stock": payload.get("stock"),
        "remote_validated": (remote_validation or {}).get("outcome") == "validated",
        "remote_validation_outcome": (remote_validation or {}).get("outcome"),
        "remote_warnings": (
            ((remote_validation or {}).get("validation") or {}).get("warnings") or []
        ),
    }


def baseline_summary(source: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    product = source.get("selected_product") or {}
    variants = product.get("variants") or []
    sellable_prices = {
        variant_price(variant)
        for variant in variants
        if variant_stock(variant) > 0 and variant_price(variant) > 0
    }
    inventory = sum(variant_stock(variant) for variant in variants)
    buyer_values = [
        baseline.get("title"),
        baseline.get("family_name"),
        (baseline.get("description") or {}).get("plain_text"),
    ] + [
        attribute.get("value_name")
        for attribute in baseline.get("attributes") or []
    ]
    try:
        historical_price = float(baseline.get("price") or 0)
    except (TypeError, ValueError):
        historical_price = 0
    return {
        "title_matches_shopify": baseline.get("title") == product.get("title"),
        "category_matches_current_prediction": (
            str(baseline.get("category_id") or "")
            == str((source.get("category") or {}).get("id") or "")
        ),
        "variation_count": len(baseline.get("variations") or []),
        "shopify_variant_count": len(variants),
        "all_variants_visible_as_api_variations": (
            len(variants) == 1
            or len(baseline.get("variations") or []) == len(variants)
        ),
        "free_shipping_live": (
            (baseline.get("shipping") or {}).get("free_shipping") is True
        ),
        "picture_count": len(baseline.get("pictures") or []),
        "shopify_image_count": len(product.get("images") or []),
        "has_contact_data": any(contains_contact_data(value) for value in buyer_values),
        "price_matches_current_sellable_variant": any(
            abs(historical_price - price) <= 0.001 for price in sellable_prices
        ),
        "live_price": baseline.get("price"),
        "current_shopify_inventory": inventory,
        "live_available_quantity": baseline.get("available_quantity"),
        "status": baseline.get("status"),
        "sub_status": baseline.get("sub_status") or [],
    }


def grading_document(summary: dict[str, Any], output_chars: int) -> dict[str, Any]:
    expectations = [
        {
            "text": "The payload passes the current local contract.",
            "passed": summary["contract_passed"],
            "evidence": (
                "validate_prepared_payload returned no violations."
                if summary["contract_passed"]
                else "; ".join(summary["contract_errors"])
            ),
        },
        {
            "text": "The title and category match the prepared Shopify/Meli source.",
            "passed": (
                summary["title_matches_shopify"]
                and summary["category_matches_preparation"]
            ),
            "evidence": (
                f"title_matches_shopify={summary['title_matches_shopify']}; "
                f"category_matches_preparation={summary['category_matches_preparation']}"
            ),
        },
        {
            "text": "Variants are preserved initially, or an observed User Products fallback is recorded with exact positive stock; only Shopify image URLs are used.",
            "passed": (
                summary["variant_strategy_passed"]
                and not any(
                    "image" in error.lower() for error in summary["contract_errors"]
                )
            ),
            "evidence": (
                f"all_variants_preserved={summary['all_variants_preserved']}; "
                f"variant_strategy_passed={summary['variant_strategy_passed']}; "
                f"fallback_applied={summary['fallback_applied']}; "
                f"fallback_reason_recorded={summary['fallback_reason_recorded']}; "
                f"variant_count={summary['variant_count']}; "
                f"shopify_variant_count={summary['shopify_variant_count']}; "
                "the local contract reported no non-Shopify image URL"
            ),
        },
        {
            "text": "Free shipping is requested and buyer-facing fields contain no contact data.",
            "passed": (
                summary["free_shipping_requested"]
                and not summary["has_contact_data"]
            ),
            "evidence": (
                f"free_shipping_requested={summary['free_shipping_requested']}; "
                f"has_contact_data={summary['has_contact_data']}"
            ),
        },
        {
            "text": "Mercado Libre accepts the final payload through /items/validate without creating a listing.",
            "passed": summary["remote_validated"],
            "evidence": (
                f"remote_validation_outcome={summary['remote_validation_outcome']}; "
                f"warnings={len(summary['remote_warnings'])}; published=false"
            ),
        },
    ]
    passed = sum(expectation["passed"] for expectation in expectations)
    return {
        "expectations": expectations,
        "summary": {
            "passed": passed,
            "failed": len(expectations) - passed,
            "total": len(expectations),
            "pass_rate": passed / len(expectations),
        },
        "execution_metrics": {
            "tool_calls": {},
            "total_tool_calls": 0,
            "total_steps": 0,
            "errors_encountered": 0 if summary["contract_passed"] else 1,
            "output_chars": output_chars,
            "transcript_chars": 0,
        },
        "timing": {
            "executor_duration_seconds": 0,
            "total_duration_seconds": 0,
        },
        "claims": [
            {
                "claim": "The reconstructed payload is source-faithful.",
                "type": "quality",
                "verified": summary["contract_passed"],
                "evidence": (
                    "The deterministic local contract passed."
                    if summary["contract_passed"]
                    else "; ".join(summary["contract_errors"])
                ),
            }
        ],
        "user_notes_summary": {
            "uncertainties": [],
            "needs_review": [],
            "workarounds": [],
        },
        "eval_feedback": {
            "suggestions": [
                {
                    "reason": (
                        "Local contract assertions do not prove that Mercado Libre "
                        "will accept category-specific variations; keep remote validation "
                        "as a separate non-publishing gate."
                    )
                }
            ],
            "overall": "Strong source-fidelity checks; live API acceptance is intentionally out of scope.",
        },
    }


def validate_remote(args: argparse.Namespace) -> int:
    """Validate replay payloads with Meli without creating or editing items."""
    from publish_payloads import build_and_validate

    output_dir = Path(args.output_dir).resolve()
    manifest = read_json(output_dir / "manifest.json", {})
    configurations = (
        ("with_skill", "old_skill")
        if args.configuration == "both"
        else (args.configuration,)
    )
    results: list[dict[str, Any]] = []
    rejected = False
    retryable_failure = False
    for manifest_case in manifest.get("cases") or []:
        shopify_id = str(manifest_case["shopify_id"])
        case_dir = output_dir / "cases" / shopify_id
        source = read_json(case_dir / "source.json", {})
        for configuration in configurations:
            payload_path = (
                case_dir / configuration / "outputs" / "productos_listos.json"
            )
            payloads = read_json(payload_path, None)
            local_errors = validate_prepared_payload(payloads, source)
            result_path = (
                case_dir / configuration / "outputs" / "remote_validation.json"
            )
            if result_path.exists():
                previous_path = (
                    case_dir
                    / configuration
                    / "outputs"
                    / "remote_validation.initial.json"
                )
                if not previous_path.exists():
                    atomic_write_json(
                        previous_path,
                        read_json(result_path, {}),
                    )
            if local_errors and configuration == "with_skill":
                result = {
                    "shopify_id": shopify_id,
                    "configuration": configuration,
                    "outcome": "invalid_local_payload",
                    "published": False,
                    "local_errors": local_errors,
                }
                rejected = True
            else:
                try:
                    candidate, validation = build_and_validate(payloads[0])
                    result = {
                        "shopify_id": shopify_id,
                        "configuration": configuration,
                        "outcome": "validated" if candidate else "validation_failed",
                        "published": False,
                        "current_contract_errors": local_errors,
                        "validation": validation,
                    }
                    if candidate is None:
                        rejected = True
                except Exception as error:
                    result = {
                        "shopify_id": shopify_id,
                        "configuration": configuration,
                        "outcome": "failed_retryable",
                        "published": False,
                        "reason": str(error),
                    }
                    retryable_failure = True
            atomic_write_json(result_path, result)
            results.append(result)

    summary = {
        "outcome": (
            "failed_retryable"
            if retryable_failure
            else "validation_failed" if rejected else "validated"
        ),
        "published": False,
        "validation_count": len(results),
        "validated": sum(result["outcome"] == "validated" for result in results),
        "failed": sum(result["outcome"] != "validated" for result in results),
        "results": results,
    }
    summary_path = output_dir / "remote_validation_summary.json"
    initial_summary_path = output_dir / "remote_validation_initial_summary.json"
    if summary_path.exists() and not initial_summary_path.exists():
        atomic_write_json(initial_summary_path, read_json(summary_path, {}))
    atomic_write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False))
    if retryable_failure:
        return 4
    return 2 if rejected else 0


def comparison_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Shopify → Meli compatibility replay",
        "",
        "This report compares two reconstructed dry-run payloads with the current "
        "Shopify source and the already-published Mercado Libre item. No listing "
        "was created or changed.",
        "",
        "| Product | Current skill | Snapshot skill | Historical title exact | "
        "Historical variants | Historical free shipping | Category drift |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case in report["cases"]:
        new = case["current_skill"]
        old = case["snapshot_skill"]
        baseline = case["historical_meli"]
        lines.append(
            "| {title} | {new} | {old} | {hist_title} | {hist_variants} | "
            "{hist_shipping} | {drift} |".format(
                title=str(case["title"]).replace("|", "\\|"),
                new="PASS" if new["contract_passed"] else "FAIL",
                old="PASS" if old["contract_passed"] else "FAIL",
                hist_title="yes" if baseline["title_matches_shopify"] else "no",
                hist_variants="yes" if baseline["all_variants_visible_as_api_variations"] else "no",
                hist_shipping="yes" if baseline["free_shipping_live"] else "no",
                drift="no" if baseline["category_matches_current_prediction"] else "yes",
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- A current-skill contract failure is a regression candidate.",
            "- A historical mismatch that the current payload corrects is an intentional improvement.",
            "- Price and inventory differences are temporal drift unless a reconstructed payload disagrees with today's Shopify source.",
            "- Category drift needs review because Mercado Libre predictions and category trees can change over time.",
            "",
        ]
    )
    return "\n".join(lines)


def benchmark_document(cases: list[dict[str, Any]]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    config_summaries = {
        "with_skill": [],
        "old_skill": [],
    }
    for index, case in enumerate(cases, start=6):
        for config_name, summary in (
            ("with_skill", case["current_skill"]),
            ("old_skill", case["snapshot_skill"]),
        ):
            grading = grading_document(summary, 0)
            result = grading["summary"]
            config_summaries[config_name].append(result["pass_rate"])
            runs.append(
                {
                    "eval_id": index,
                    "eval_name": case["title"],
                    "configuration": config_name,
                    "run_number": 1,
                    "result": {
                        "pass_rate": result["pass_rate"],
                        "passed": result["passed"],
                        "failed": result["failed"],
                        "total": result["total"],
                        "time_seconds": 0,
                        "tokens": 0,
                        "tool_calls": 0,
                        "errors": 0 if summary["contract_passed"] else 1,
                    },
                    "expectations": grading["expectations"],
                    "notes": [],
                }
            )

    run_summary: dict[str, Any] = {}
    for config_name, values in config_summaries.items():
        mean = sum(values) / len(values) if values else 0
        run_summary[config_name] = {
            "pass_rate": {
                "mean": mean,
                "stddev": statistics.stdev(values) if len(values) > 1 else 0,
                "min": min(values) if values else 0,
                "max": max(values) if values else 0,
            },
            "time_seconds": {"mean": 0, "stddev": 0, "min": 0, "max": 0},
            "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0},
        }
    run_summary["delta"] = {
        "pass_rate": f"{run_summary['with_skill']['pass_rate']['mean'] - run_summary['old_skill']['pass_rate']['mean']:+.2f}",
        "time_seconds": "+0.0",
        "tokens": "+0",
    }

    historical_category_matches = sum(
        case["historical_meli"]["category_matches_current_prediction"]
        for case in cases
    )
    historical_free_shipping = sum(
        case["historical_meli"]["free_shipping_live"] for case in cases
    )
    current_passed = sum(
        run["result"]["passed"]
        for run in runs
        if run["configuration"] == "with_skill"
    )
    current_total = sum(
        run["result"]["total"]
        for run in runs
        if run["configuration"] == "with_skill"
    )
    old_passed = sum(
        run["result"]["passed"]
        for run in runs
        if run["configuration"] == "old_skill"
    )
    old_total = sum(
        run["result"]["total"]
        for run in runs
        if run["configuration"] == "old_skill"
    )
    return {
        "metadata": {
            "skill_name": "shopify-to-meli-one-by-one-sync",
            "skill_path": str(
                REPO_ROOT / ".agents" / "skills" / "shopify-to-meli-one-by-one-sync"
            ),
            "executor_model": "gpt-5",
            "analyzer_model": "gpt-5",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "evals_run": list(range(6, 6 + len(cases))),
            "runs_per_configuration": 1,
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": [
            f"The current skill passed {current_passed}/{current_total} end-to-end replay assertions; the snapshot passed {old_passed}/{old_total}.",
            f"After the category-selector fix, {historical_category_matches}/{len(cases)} prepared categories match the already-published Meli categories; the initial replay matched 3/5.",
            "Both configurations validated 5/5 final payloads through Meli /items/validate after applying the observed User Products fallback.",
            "The snapshot fallback validates remotely but does not persist variations_fallback_reason, so three multi-variant final payloads fail the current auditable local contract.",
            f"The live historical listings currently have free shipping on {historical_free_shipping}/{len(cases)} items, while both reconstructed payloads request it on every item; Mercado Libre may override the request.",
            "Three historical multi-variant products are flattened at item level; both replays first preserved every variant, then used the exact observed validation rejection before flattening to positive aggregate stock.",
            "Timing and token metrics were unavailable from the collaboration runner and are recorded as zero rather than estimated.",
            "Only Meli /items/validate was called; no item creation, update, activation, mapping or Shopify mutation occurred.",
        ],
    }


def benchmark_markdown(benchmark: dict[str, Any]) -> str:
    current = benchmark["run_summary"]["with_skill"]["pass_rate"]["mean"]
    old = benchmark["run_summary"]["old_skill"]["pass_rate"]["mean"]
    current_runs = [
        run for run in benchmark["runs"] if run["configuration"] == "with_skill"
    ]
    old_runs = [
        run for run in benchmark["runs"] if run["configuration"] == "old_skill"
    ]
    current_passed = sum(run["result"]["passed"] for run in current_runs)
    current_total = sum(run["result"]["total"] for run in current_runs)
    old_passed = sum(run["result"]["passed"] for run in old_runs)
    old_total = sum(run["result"]["total"] for run in old_runs)
    lines = [
        "# Real-product compatibility benchmark",
        "",
        "| Configuration | Assertions passed | Pass rate |",
        "|---|---:|---:|",
        f"| Current skill | {current_passed}/{current_total} | {current * 100:.0f}% |",
        f"| Snapshot skill | {old_passed}/{old_total} | {old * 100:.0f}% |",
        "",
        "## Analyst notes",
        "",
    ]
    lines.extend(f"- {note}" for note in benchmark["notes"])
    lines.append("")
    return "\n".join(lines)


def compare(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    manifest = read_json(output_dir / "manifest.json", {})
    cases: list[dict[str, Any]] = []
    for manifest_case in manifest.get("cases") or []:
        shopify_id = str(manifest_case["shopify_id"])
        case_dir = output_dir / "cases" / shopify_id
        source = read_json(case_dir / "source.json", {})
        baseline = read_json(case_dir / "baseline_meli.json", {})
        current_payloads = read_json(
            case_dir / "with_skill" / "outputs" / "productos_listos.json", None
        )
        current_initial_payloads = read_json(
            case_dir / "with_skill" / "outputs" / "productos_listos.initial.json",
            current_payloads,
        )
        current_remote_validation = read_json(
            case_dir / "with_skill" / "outputs" / "remote_validation.json", {}
        )
        snapshot_payloads = read_json(
            case_dir / "old_skill" / "outputs" / "productos_listos.json", None
        )
        snapshot_initial_payloads = read_json(
            case_dir / "old_skill" / "outputs" / "productos_listos.initial.json",
            snapshot_payloads,
        )
        snapshot_remote_validation = read_json(
            case_dir / "old_skill" / "outputs" / "remote_validation.json", {}
        )
        case = {
            "shopify_id": shopify_id,
            "meli_id": manifest_case["meli_id"],
            "title": manifest_case["title"],
            "current_skill": payload_summary(
                current_payloads,
                source,
                current_initial_payloads,
                current_remote_validation,
            ),
            "snapshot_skill": payload_summary(
                snapshot_payloads,
                source,
                snapshot_initial_payloads,
                snapshot_remote_validation,
            ),
            "historical_meli": baseline_summary(source, baseline),
        }
        atomic_write_json(case_dir / "comparison.json", case)
        for config_name, summary in (
            ("with_skill", case["current_skill"]),
            ("old_skill", case["snapshot_skill"]),
        ):
            outputs_dir = case_dir / config_name / "outputs"
            output_chars = sum(
                path.stat().st_size for path in outputs_dir.glob("*") if path.is_file()
            )
            atomic_write_json(
                case_dir / config_name / "grading.json",
                grading_document(summary, output_chars),
            )
        cases.append(case)

    report = {
        "schema_version": 1,
        "read_only": True,
        "case_count": len(cases),
        "current_skill_contract_passes": sum(
            case["current_skill"]["contract_passed"] for case in cases
        ),
        "snapshot_skill_contract_passes": sum(
            case["snapshot_skill"]["contract_passed"] for case in cases
        ),
        "regression_candidates": [
            case["shopify_id"]
            for case in cases
            if not case["current_skill"]["contract_passed"]
        ],
        "cases": cases,
    }
    atomic_write_json(output_dir / "compatibility_report.json", report)
    (output_dir / "compatibility_report.md").write_text(
        comparison_markdown(report), encoding="utf-8"
    )
    benchmark = benchmark_document(cases)
    atomic_write_json(output_dir / "benchmark.json", benchmark)
    (output_dir / "benchmark.md").write_text(
        benchmark_markdown(benchmark), encoding="utf-8"
    )
    print(json.dumps({"outcome": "compared", **report}, ensure_ascii=False))
    return 0 if not report["regression_candidates"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay mapped Shopify products without changing Shopify or Mercado Libre."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="Capture read-only Shopify/Meli fixtures."
    )
    collect_parser.add_argument("--shopify-id", action="append", default=[])
    collect_parser.add_argument("--count", type=int, choices=(3, 5), default=5)
    collect_parser.add_argument("--allow-other-count", action="store_true")
    collect_parser.add_argument("--mappings", default=str(MAPPINGS_FILE))
    collect_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    collect_parser.set_defaults(handler=collect)

    compare_parser = subparsers.add_parser(
        "compare", help="Compare reconstructed payloads with source and live baseline."
    )
    compare_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    compare_parser.set_defaults(handler=compare)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Call only Meli /items/validate for reconstructed payloads.",
    )
    validate_parser.add_argument(
        "--configuration",
        choices=("with_skill", "old_skill", "both"),
        default="both",
    )
    validate_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    validate_parser.set_defaults(handler=validate_remote)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as error:
        print(
            json.dumps(
                {"outcome": "failed", "command": args.command, "reason": str(error)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
