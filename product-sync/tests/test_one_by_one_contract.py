import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from one_by_one_contract import (  # noqa: E402
    eligibility_reason,
    sanitize_meli_title,
    select_first_eligible,
    validate_prepared_payload,
)


def product():
    return {
        "id": 101,
        "title": "Adaptador USB-C",
        "status": "active",
        "vendor": "Zipp",
        "meli_brand": "Genérica",
        "images": [
            {"src": "https://cdn.shopify.com/a.jpg"},
            {"src": "https://cdn.shopify.com/b.jpg"},
        ],
        "variants": [
            {
                "id": 201,
                "title": "Gris",
                "price": "12990",
                "inventory_quantity": 2,
                "sku": "A-GRIS",
            },
            {
                "id": 202,
                "title": "Negro",
                "price": "12990",
                "inventory_quantity": 0,
                "sku": "A-NEGRO",
            },
        ],
    }


def source():
    return {
        "selected_product": product(),
        "category": {"id": "MLC1"},
        "category_attributes": [
            {"id": "COLOR", "tags": {"required": True}},
            {"id": "MATERIAL", "tags": {"recommended": True}},
        ],
    }


def payload():
    return {
        "shopify_id": "101",
        "category_id": "MLC1",
        "original_title": "Adaptador USB-C",
        "price": 12990,
        "stock": 2,
        "barcode": "No aplica",
        "images": ["https://cdn.shopify.com/a.jpg", "https://cdn.shopify.com/b.jpg"],
        "shipping": {"mode": "me2", "local_pick_up": True, "free_shipping": True},
        "variations": [
            {
                "shopify_variant_id": "201",
                "sku": "A-GRIS",
                "price": 12990,
                "stock": 2,
                "attribute_combinations": [{"id": "COLOR", "value_name": "Gris"}],
                "images": ["https://cdn.shopify.com/a.jpg"],
            },
            {
                "shopify_variant_id": "202",
                "sku": "A-NEGRO",
                "price": 12990,
                "stock": 0,
                "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}],
                "images": ["https://cdn.shopify.com/b.jpg"],
            },
        ],
        "ai_data": {
            "optimized_title": "Adaptador USB-C",
            "clean_description": "Adaptador para audio.\n\nCaracterísticas del producto:\nConector USB-C.",
            "brand": "Genérica",
            "model": "Adaptador USB-C",
        },
        "model_evidence": {
            "source": "shopify_title_explicit_model",
            "quote": "Adaptador USB-C",
        },
        "extra_attributes": [],
    }


class EligibilityTests(unittest.TestCase):
    def test_requires_stock_and_price_on_the_same_variant(self):
        candidate = product()
        candidate["variants"] = [
            {"id": 1, "price": 0, "inventory_quantity": 2},
            {"id": 2, "price": 1000, "inventory_quantity": 0},
        ]
        self.assertEqual(
            eligibility_reason(candidate, {}, {}),
            "no_sellable_variant",
        )

    def test_skips_mapped_and_blocked_products_before_selecting_one(self):
        mapped = product()
        blocked = {**product(), "id": 102}
        selected = {**product(), "id": 103}
        result, skipped = select_first_eligible(
            [mapped, blocked, selected],
            {"101": "MLC1"},
            {"102": {"do_not_auto_republish": True}},
        )
        self.assertEqual(result["id"], 103)
        self.assertEqual([item["reason"] for item in skipped], ["already_synced", "blocked"])

    def test_mapped_family_is_not_republished_when_stock_changes(self):
        candidate = product()
        candidate["variants"][1]["inventory_quantity"] = 1
        mapping = {
            "mode": "user_products_family",
            "variants": {"201": {"meli_item_id": "MLC1"}},
        }
        self.assertEqual(eligibility_reason(candidate, {"101": mapping}, {}), "already_synced")

    def test_sanitizes_only_zipp_store_trace_from_title(self):
        self.assertEqual(
            sanitize_meli_title("Cable USB-C Zipp Chile"),
            "Cable USB-C",
        )


class PayloadContractTests(unittest.TestCase):
    def test_rejects_model_without_explicit_shopify_evidence(self):
        candidate = payload()
        candidate.pop("model_evidence")
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("model evidence" in error for error in errors))

    def test_rejects_connector_pair_as_model_even_with_title_evidence(self):
        candidate = payload()
        candidate["ai_data"]["model"] = "USB-C a Lightning"
        candidate["model_evidence"] = {
            "source": "shopify_title_explicit_model",
            "quote": "Adaptador USB-C",
        }
        candidate["extra_attributes"] = [
            {"id": "INPUT_CONNECTOR", "value_name": "USB-C"},
            {"id": "OUTPUT_CONNECTOR", "value_name": "Lightning"},
        ]
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("connector pair" in error for error in errors))

    def test_valid_payload_passes(self):
        self.assertEqual(validate_prepared_payload([payload()], source()), [])

    def test_rejects_more_than_one_product(self):
        self.assertIn("exactly one", validate_prepared_payload([payload(), payload()], source())[0])

    def test_rejects_contact_data_and_non_shopify_images(self):
        candidate = payload()
        candidate["ai_data"]["brand"] = "Zipp.cl"
        candidate["images"].append("https://example.com/generated.jpg")
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("contact data" in error for error in errors))
        self.assertTrue(any("Shopify originals" in error for error in errors))

    def test_allows_audited_background_remediation_after_meli_rejection(self):
        candidate = payload()
        candidate["images"] = [
            "https://images.meli-remediation.local/white.jpg",
            "https://cdn.shopify.com/a.jpg",
        ]
        candidate["image_provenance"] = [
            {
                "source_url": "https://cdn.shopify.com/a.jpg",
                "output_url": "https://images.meli-remediation.local/white.jpg",
                "transformation": "background_removal",
                "source_checksum": "sha256:source",
                "output_checksum": "sha256:output",
                "meli_rejection": "picture requires white background",
            }
        ]
        self.assertEqual(validate_prepared_payload([candidate], source()), [])

    def test_rejects_shopify_vendor_as_meli_brand_without_override(self):
        candidate = payload()
        candidate["ai_data"]["brand"] = "Zipp"
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("brand" in error or "Zipp" in error for error in errors))

    def test_rejects_inventory_inflation_and_missing_variants(self):
        candidate = payload()
        candidate["stock"] = 50
        candidate["variations"] = candidate["variations"][:1]
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("every Shopify variant" in error for error in errors))
        self.assertTrue(any("cannot exceed" in error for error in errors))

    def test_title_change_requires_an_explicit_allowed_reason(self):
        candidate = payload()
        candidate["ai_data"]["optimized_title"] = "Adaptador USB-C mejorado"
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("documented Meli rejection" in error for error in errors))
        candidate["title_change_reason"] = "meli_title_limit"
        candidate["title_rejection_evidence"] = {
            "code": "item.title.invalid",
            "message": "title exceeds category limit",
        }
        self.assertFalse(
            any("documented Meli rejection" in error for error in validate_prepared_payload([candidate], source()))
        )

    def test_title_with_zipp_requires_sanitized_initial_title(self):
        candidate_source = source()
        candidate_source["selected_product"]["title"] = "Adaptador USB-C Zipp"
        candidate = payload()
        candidate["original_title"] = "Adaptador USB-C Zipp"
        candidate["ai_data"]["optimized_title"] = "Adaptador USB-C"
        candidate["title_change_reason"] = "store_brand_policy"
        self.assertEqual(validate_prepared_payload([candidate], candidate_source), [])

    def test_rejects_omitting_a_zero_stock_variant(self):
        candidate_source = source()
        candidate_source["selected_product"]["variants"][1]["inventory_quantity"] = 1
        candidate = payload()
        candidate["variations"][1]["stock"] = 1
        candidate["variations"][1]["images"] = []
        candidate["variations"][1]["publication_disposition"] = "omit"
        candidate["variations"][1]["omit_reason"] = "pending_visual_evidence"
        self.assertTrue(any("cannot be omitted" in error for error in validate_prepared_payload([candidate], candidate_source)))

    def test_unique_exact_sku_match_requires_explicit_adoption(self):
        candidate_source = source()
        candidate_source["sku_reconciliation"] = {
            "variants": {
                "201": {
                    "sku": "A-GRIS",
                    "matches": [{"meli_item_id": "MLC-EXISTING"}],
                }
            }
        }
        candidate = payload()
        errors = validate_prepared_payload([candidate], candidate_source)
        self.assertTrue(any("explicit adoption" in error for error in errors))
        candidate["reconciliation"] = {
            "201": {"decision": "adopt", "meli_item_id": "MLC-EXISTING"}
        }
        self.assertEqual(validate_prepared_payload([candidate], candidate_source), [])

    def test_ambiguous_exact_sku_matches_are_rejected(self):
        candidate_source = source()
        candidate_source["sku_reconciliation"] = {
            "variants": {
                "201": {
                    "sku": "A-GRIS",
                    "matches": [
                        {"meli_item_id": "MLC-1"},
                        {"meli_item_id": "MLC-2"},
                    ],
                }
            }
        }
        errors = validate_prepared_payload([payload()], candidate_source)
        self.assertTrue(any("ambiguous existing Meli SKU matches" in error for error in errors))

    def test_rejects_category_and_top_level_price_drift(self):
        candidate = payload()
        candidate["category_id"] = "MLC-WRONG"
        candidate["price"] = 99999
        errors = validate_prepared_payload([candidate], source())
        self.assertTrue(any("category_id" in error for error in errors))
        self.assertTrue(any("sellable Shopify variant price" in error for error in errors))

    def test_rejects_duplicate_variant_and_changed_barcode(self):
        candidate_source = source()
        candidate_source["selected_product"]["variants"][0]["barcode"] = "7801234567890"
        candidate = payload()
        candidate["variations"][0]["barcode"] = "0000000000000"
        candidate["variations"].append(dict(candidate["variations"][1]))
        errors = validate_prepared_payload([candidate], candidate_source)
        self.assertTrue(any("exactly once" in error for error in errors))
        self.assertTrue(any("barcode differs" in error for error in errors))

    def test_requires_variations_for_multiple_shopify_variants(self):
        candidate_source = source()
        candidate_source["selected_product"]["variants"][1]["inventory_quantity"] = 1
        candidate = payload()
        candidate.pop("variations")
        candidate["stock"] = 3
        errors = validate_prepared_payload([candidate], candidate_source)
        self.assertTrue(any("must include variations" in error for error in errors))

    def test_all_variants_require_sku_image_and_option_combination(self):
        candidate = payload()
        candidate_source = source()
        candidate_source["selected_product"]["variants"][1]["inventory_quantity"] = 1
        candidate["variations"][1]["stock"] = 1
        candidate["variations"][0]["sku"] = ""
        candidate["variations"][0]["images"] = []
        candidate["variations"][0]["attribute_combinations"] = []
        errors = validate_prepared_payload([candidate], candidate_source)
        self.assertTrue(any("requires a Shopify SKU" in error for error in errors))
        self.assertTrue(any("requires an assigned Shopify image" in error for error in errors))
        self.assertTrue(any("unambiguous Meli option" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
