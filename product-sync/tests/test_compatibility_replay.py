import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "compatibility_replay", SCRIPT_DIR / "compatibility_replay.py"
)
compatibility_replay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(compatibility_replay)


def source_fixture():
    return {
        "selected_product": {
            "id": 1,
            "title": "Cable USB-C",
            "images": [{"src": "https://cdn.shopify.com/a.jpg"}],
            "variants": [
                {
                    "id": 11,
                    "price": "1000",
                    "inventory_quantity": 2,
                    "sku": "CAB-USB-C-1",
                    "barcode": "",
                }
            ],
        },
        "category": {"id": "MLC1"},
        "category_attributes": [],
    }


def valid_payload():
    return [
        {
            "shopify_id": "1",
            "category_id": "MLC1",
            "original_title": "Cable USB-C",
            "price": 1000,
            "stock": 2,
            "barcode": "No aplica",
            "images": ["https://cdn.shopify.com/a.jpg"],
            "shipping": {
                "mode": "me2",
                "local_pick_up": True,
                "free_shipping": True,
            },
            "variations": [
                {
                    "shopify_variant_id": "11",
                    "sku": "CAB-USB-C-1",
                    "price": 1000,
                    "stock": 2,
                    "barcode": "",
                    "attribute_combinations": [],
                    "images": ["https://cdn.shopify.com/a.jpg"],
                }
            ],
            "ai_data": {
                "optimized_title": "Cable USB-C",
                "clean_description": "Características del producto:\nCable USB-C.",
                "brand": "Genérica",
                "model": "Cable",
            },
            "model_evidence": {
                "source": "shopify_title_explicit_model",
                "quote": "Cable USB-C",
            },
            "extra_attributes": [],
        }
    ]


class CompatibilityReplayTests(unittest.TestCase):
    def test_payload_summary_accepts_source_faithful_payload(self):
        summary = compatibility_replay.payload_summary(
            valid_payload(), source_fixture()
        )
        self.assertTrue(summary["contract_passed"])
        self.assertTrue(summary["title_matches_shopify"])
        self.assertTrue(summary["all_variants_preserved"])
        self.assertTrue(summary["free_shipping_requested"])

    def test_payload_summary_reports_contract_regression(self):
        payload = valid_payload()
        payload[0]["shipping"]["free_shipping"] = False
        summary = compatibility_replay.payload_summary(payload, source_fixture())
        self.assertFalse(summary["contract_passed"])
        self.assertIn(
            "shipping.free_shipping must default to true",
            summary["contract_errors"],
        )

    def test_single_variant_can_be_represented_at_item_level(self):
        payload = valid_payload()
        payload[0]["variations"] = []
        summary = compatibility_replay.payload_summary(payload, source_fixture())
        self.assertTrue(summary["contract_passed"])
        self.assertTrue(summary["all_variants_preserved"])

    def test_baseline_summary_marks_temporal_and_historical_differences(self):
        baseline = {
            "title": "Cable Usb-c Gris",
            "category_id": "MLC2",
            "price": 900,
            "available_quantity": 1,
            "status": "active",
            "sub_status": [],
            "shipping": {"free_shipping": False},
            "attributes": [],
            "variations": [],
            "pictures": [{"id": "x"}],
            "description": {"plain_text": "Cable"},
        }
        summary = compatibility_replay.baseline_summary(source_fixture(), baseline)
        self.assertFalse(summary["title_matches_shopify"])
        self.assertFalse(summary["category_matches_current_prediction"])
        self.assertFalse(summary["free_shipping_live"])
        self.assertFalse(summary["price_matches_current_sellable_variant"])

    def test_observed_aggregated_variant_fallback_is_rejected(self):
        source = source_fixture()
        source["selected_product"]["variants"].append(
            {
                "id": 12,
                "price": "1000",
                "inventory_quantity": 0,
                "barcode": "",
            }
        )
        initial = valid_payload()
        initial[0]["variations"].append(
            {
                "shopify_variant_id": "12",
                "price": 1000,
                "stock": 0,
                "barcode": "",
                "attribute_combinations": [],
                "images": ["https://cdn.shopify.com/a.jpg"],
            }
        )
        final = valid_payload()
        final[0]["variations"] = []
        final[0]["variations_fallback_reason"] = (
            "The field variations is invalid with family name"
        )
        remote = {"outcome": "validated", "validation": {"warnings": []}}

        summary = compatibility_replay.payload_summary(
            final, source, initial, remote
        )
        self.assertFalse(summary["contract_passed"])
        self.assertFalse(summary["variant_strategy_passed"])
        self.assertTrue(summary["fallback_stock_matches_positive_inventory"])
        self.assertTrue(summary["remote_validated"])
        self.assertTrue(any("must include variations" in error for error in summary["contract_errors"]))

        final[0].pop("variations_fallback_reason")
        unrecorded = compatibility_replay.payload_summary(
            final, source, initial, remote
        )
        self.assertFalse(unrecorded["contract_passed"])
        self.assertFalse(unrecorded["variant_strategy_passed"])


if __name__ == "__main__":
    unittest.main()
