import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import one_by_one_sync  # noqa: E402
import publish_payloads  # noqa: E402
from one_by_one_contract import workflow_lock  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class MappingReconciliationTests(unittest.TestCase):
    def test_removes_only_items_proven_missing_or_deleted(self):
        items = {
            "MLC1": {"status": "active", "sub_status": []},
            "MLC2": {"status": "inactive", "sub_status": ["deleted"]},
            "MLC3": None,
        }
        reconciled, removed = one_by_one_sync.reconcile_mappings(
            {"1": "MLC1", "2": "MLC2", "3": "MLC3"},
            fetch_item=lambda item_id: items[item_id],
        )
        self.assertEqual(reconciled, {"1": "MLC1"})
        self.assertEqual({entry["shopify_id"] for entry in removed}, {"2", "3"})

    def test_keeps_family_mapping_when_only_one_member_is_missing(self):
        family_mapping = {
            "mode": "user_products_family",
            "variants": {
                "v1": {"meli_item_id": "MLC1"},
                "v2": {"meli_item_id": "MLC2"},
            },
        }
        reconciled, removed = one_by_one_sync.reconcile_mappings(
            {"1": family_mapping},
            fetch_item=lambda item_id: None if item_id == "MLC2" else {"status": "active", "sub_status": []},
        )
        self.assertEqual(reconciled, {"1": family_mapping})
        self.assertEqual(removed, [])

    @patch.object(one_by_one_sync, "load_blocked_products", return_value={})
    @patch.object(
        one_by_one_sync,
        "reconcile_seller_skus",
        return_value={"seller_id": "1", "variants": {}},
    )
    @patch.object(
        one_by_one_sync,
        "get_shopify_products",
        return_value={"products": []},
    )
    @patch.object(
        one_by_one_sync,
        "reconcile_mappings",
        return_value=({}, [{"shopify_id": "1", "meli_id": "MLC1"}]),
    )
    def test_dry_run_prepare_does_not_rewrite_mapping(
        self, _reconcile, _products, _sku_reconcile, _blocked
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            mappings = directory / "mappings.json"
            original = '{\n  "1": "MLC1"\n}\n'
            mappings.write_text(original, encoding="utf-8")
            args = SimpleNamespace(
                work_dir=str(directory / "work"),
                mappings=str(mappings),
                blocked_products=str(directory / "blocked.json"),
                reservations=str(directory / "reservations.json"),
                journal=str(directory / "journal.json"),
                mode="dry-run",
                limit=250,
            )
            self.assertEqual(one_by_one_sync.prepare(args), 0)
            self.assertEqual(mappings.read_text(encoding="utf-8"), original)

    @patch.object(one_by_one_sync, "get_shopify_products")
    def test_prepare_resumes_existing_reservation_before_selecting(self, products):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            work = directory / "work"
            work.mkdir()
            (work / "source.json").write_text(
                json.dumps(runtime_source("publish")), encoding="utf-8"
            )
            (directory / "mappings.json").write_text("{}", encoding="utf-8")
            (directory / "reservations.json").write_text(
                json.dumps({"test-101": {"stage": "prepared"}}),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                work_dir=str(work),
                mappings=str(directory / "mappings.json"),
                blocked_products=str(directory / "blocked.json"),
                reservations=str(directory / "reservations.json"),
                journal=str(directory / "journal.json"),
                mode="publish",
                limit=250,
            )
            self.assertEqual(one_by_one_sync.prepare(args), 0)
            products.assert_not_called()

    def test_process_lock_rejects_overlapping_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "workflow.lock"
            with workflow_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with workflow_lock(lock_path):
                        pass

    @patch.object(one_by_one_sync, "load_blocked_products", return_value={})
    @patch.object(
        one_by_one_sync,
        "reconcile_seller_skus",
        return_value={"seller_id": "1", "variants": {}},
    )
    @patch.object(one_by_one_sync, "get_category_attributes", return_value=[])
    @patch.object(
        one_by_one_sync,
        "predict_meli_category",
        return_value=[
            {
                "category_id": "MLC1",
                "category_name": "Adaptadores",
                "domain_id": "MLC-ADAPTERS",
            }
        ],
    )
    @patch.object(
        one_by_one_sync,
        "get_shopify_products",
        return_value={
            "products": [
                {
                    "id": "test-101",
                    "title": "Adaptador USB-C",
                    "status": "active",
                    "images": [{"src": "https://cdn.shopify.com/a.jpg"}],
                    "variants": [
                        {
                            "id": "v1",
                            "price": "12990",
                            "inventory_quantity": 2,
                        }
                    ],
                }
            ]
        },
    )
    @patch.object(one_by_one_sync, "get_product_metafield", return_value=None)
    @patch.object(one_by_one_sync, "reconcile_mappings", return_value=({}, []))
    def test_publish_prepare_creates_reservation(
        self, _reconcile, _brand, _products, _predict, _attributes, _sku_reconcile, _blocked
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "mappings.json").write_text("{}", encoding="utf-8")
            args = SimpleNamespace(
                work_dir=str(directory / "work"),
                mappings=str(directory / "mappings.json"),
                blocked_products=str(directory / "blocked.json"),
                reservations=str(directory / "reservations.json"),
                journal=str(directory / "journal.json"),
                mode="publish",
                limit=250,
            )
            self.assertEqual(one_by_one_sync.prepare(args), 0)
            reservations = json.loads(
                (directory / "reservations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(reservations["test-101"]["stage"], "prepared")


class CategorySelectionTests(unittest.TestCase):
    def test_category_candidates_are_distinct_and_bounded(self):
        predictions = [
            {"category_id": f"MLC{index}", "domain_id": f"D{index}"}
            for index in range(8)
        ]
        predictions.append({"category_id": "MLC1", "domain_id": "duplicate"})
        candidates = one_by_one_sync.unique_category_candidates(predictions)
        self.assertEqual(len(candidates), 5)
        self.assertEqual([item["category_id"] for item in candidates], [f"MLC{i}" for i in range(5)])

    @patch.object(
        one_by_one_sync,
        "predict_meli_category",
        return_value=[{"category_id": "MLC2", "category_name": "Cables", "domain_id": "D2"}],
    )
    def test_discover_category_records_query_and_candidate(self, _predict):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "selected_product": {"id": "1", "title": "Cable Llavero"},
                        "category_candidates": [{"category_id": "MLC1"}],
                        "category_resolution": {
                            "max_candidates": 5,
                            "queries": [{"query": "Cable Llavero", "reason": "exact_shopify_title"}],
                            "selections": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(source_path),
                query="Cable de carga multipuerto",
                reason="Primary function shown by Shopify description",
            )
            self.assertEqual(one_by_one_sync.discover_categories(args), 0)
            updated = json.loads(source_path.read_text())
            self.assertEqual(updated["category_candidates"][-1]["category_id"], "MLC2")
            self.assertEqual(updated["category_resolution"]["queries"][-1]["reason"], args.reason)

    @patch.object(
        one_by_one_sync,
        "get_category_attributes",
        return_value=[{"id": "BRAND", "tags": {"required": True}}],
    )
    def test_select_category_persists_reason_and_current_attributes(self, _attributes):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "selected_product": {"id": "1"},
                        "category_candidates": [
                            {"category_id": "MLC2", "category_name": "Cables", "domain_id": "D2"}
                        ],
                        "category_resolution": {"queries": [], "selections": []},
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                source=str(source_path),
                category_id="MLC2",
                reason="Matches product type and primary function",
            )
            self.assertEqual(one_by_one_sync.select_category(args), 0)
            updated = json.loads(source_path.read_text())
            self.assertEqual(updated["category"]["id"], "MLC2")
            self.assertEqual(updated["category_attributes"][0]["id"], "BRAND")


class PublicationGateTests(unittest.TestCase):
    def test_active_status_is_not_enough_when_warnings_exist(self):
        self.assertFalse(
            publish_payloads.live_gate_passes(
                {"status": "active", "sub_status": [], "warnings": [{"code": "x"}]}
            )
        )

    def test_requires_two_consecutive_clean_reads(self):
        reads = [
            {"status": "active", "sub_status": [], "warnings": []},
            {"status": "under_review", "sub_status": ["waiting_for_patch"], "warnings": []},
            {"status": "active", "sub_status": [], "warnings": []},
            {"status": "active", "sub_status": [], "warnings": []},
        ]
        with patch.object(publish_payloads, "fetch_item", side_effect=reads):
            result = publish_payloads.ensure_item_active(
                "MLC1", [], attempts=4, delay_seconds=0, stable_checks=2
            )
        self.assertEqual(result["status"], "active")

    def test_validation_with_only_warnings_can_continue_to_live_gate(self):
        response = FakeResponse(
            400,
            {"cause": [{"type": "warning", "code": "shipping", "message": "info"}]},
        )
        self.assertTrue(publish_payloads.validation_succeeded(response))

    def test_validation_errors_fail(self):
        response = FakeResponse(
            400,
            {"cause": [{"type": "error", "code": "required", "message": "missing"}]},
        )
        self.assertFalse(publish_payloads.validation_succeeded(response))

    def test_auth_and_rate_limit_validation_failures_are_retryable(self):
        for status_code in (401, 403, 429, 503):
            with self.subTest(status_code=status_code):
                self.assertTrue(
                    publish_payloads.validation_failure_is_retryable(
                        {"attempts": [{"status_code": status_code}]}
                    )
                )

    def test_normal_validation_error_is_not_retryable(self):
        self.assertFalse(
            publish_payloads.validation_failure_is_retryable(
                {"attempts": [{"status_code": 400}]}
            )
        )

    @patch.object(publish_payloads, "validate_item_on_meli")
    def test_user_product_variation_rejection_does_not_split_the_listing(self, validate):
        requires_family = FakeResponse(
            400,
            {"cause": [{"type": "error", "message": "family_name is required"}]},
        )
        validate.return_value = requires_family
        prepared = runtime_payload()[0]
        prepared["images"].append("https://cdn.shopify.com/b.jpg")
        prepared["stock"] = 5
        prepared["variations"] = [
            {
                "shopify_variant_id": "v-grey",
                "sku": "SKU-GREY",
                "price": 12000,
                "stock": 2,
                "images": ["https://cdn.shopify.com/a.jpg"],
                "attribute_combinations": [{"id": "COLOR", "value_name": "Gris"}],
            },
            {
                "shopify_variant_id": "v-black",
                "sku": "SKU-BLACK",
                "price": 13000,
                "stock": 3,
                "images": ["https://cdn.shopify.com/b.jpg"],
                "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}],
            },
        ]

        candidate, result = publish_payloads.build_and_validate(prepared)

        self.assertIsNone(candidate)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["format"], "standard")

    @patch.object(publish_payloads, "validate_item_on_meli", return_value=FakeResponse(204))
    def test_keeps_zero_stock_variants_in_the_single_listing(self, _validate):
        prepared = runtime_payload()[0]
        prepared["images"].append("https://cdn.shopify.com/b.jpg")
        prepared["variations"] = [
            {
                "shopify_variant_id": "v-grey", "sku": "SKU-GREY", "price": 12000,
                "stock": 2, "images": ["https://cdn.shopify.com/a.jpg"],
                "attribute_combinations": [{"id": "COLOR", "value_name": "Gris"}],
            },
            {
                "shopify_variant_id": "v-black", "sku": "SKU-BLACK", "price": 13000,
                "stock": 0, "images": ["https://cdn.shopify.com/b.jpg"],
                "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}],
            },
        ]
        candidate, _ = publish_payloads.build_and_validate(prepared)
        self.assertEqual([variant["available_quantity"] for variant in candidate["variations"]], [2, 0])
        self.assertEqual(candidate["variations"][1]["seller_custom_field"], "SKU-BLACK")

    def test_live_variation_verification_requires_a_zero_stock_variant(self):
        prepared = runtime_payload()[0]
        prepared["variations"] = [
            {"sku": "SKU-GREY", "price": 12000, "stock": 2},
            {"sku": "SKU-BLACK", "price": 13000, "stock": 0},
        ]
        live = {
            "variations": [
                {"seller_custom_field": "SKU-GREY", "price": 12000, "available_quantity": 2},
            ]
        }
        self.assertEqual(
            publish_payloads.verify_live_variations(live, prepared),
            ["missing Shopify variation SKU SKU-BLACK"],
        )


def runtime_source(mode):
    return {
        "mode": mode,
        "category": {"id": "MLC1"},
        "selected_product": {
            "id": "test-101",
            "title": "Adaptador USB-C",
            "images": [{"src": "https://cdn.shopify.com/a.jpg"}],
            "variants": [
                {
                    "id": "test-variant",
                    "price": "12990",
                    "inventory_quantity": 2,
                }
            ],
        },
        "category_attributes": [],
    }


def runtime_payload():
    return [
        {
            "shopify_id": "test-101",
            "category_id": "MLC1",
            "original_title": "Adaptador USB-C",
            "price": 12990,
            "stock": 2,
            "barcode": "No aplica",
            "images": ["https://cdn.shopify.com/a.jpg"],
            "shipping": {
                "mode": "me2",
                "local_pick_up": True,
                "free_shipping": True,
            },
            "ai_data": {
                "optimized_title": "Adaptador USB-C",
                "clean_description": (
                    "Adaptador para audio.\n\n"
                    "Características del producto:\nConector USB-C."
                ),
            "brand": "Genérica",
                "model": "Adaptador USB-C",
            },
            "model_evidence": {
                "source": "shopify_title_explicit_model",
                "quote": "Adaptador USB-C",
            },
            "extra_attributes": [],
        }
    ]


class PublicationTransactionTests(unittest.TestCase):
    def make_args(self, directory, publish):
        return SimpleNamespace(
            publish=publish,
            source=str(directory / "source.json"),
            payload=str(directory / "payload.json"),
            result=str(directory / "result.json"),
            mappings=str(directory / "mappings.json"),
            quality_records=str(directory / "quality.json"),
            blocked_products=str(directory / "blocked.json"),
            journal=str(directory / "journal.json"),
            reservations=str(directory / "reservations.json"),
            poll_attempts=2,
            poll_delay=0,
            stable_checks=1,
        )

    def write_inputs(self, directory, mode):
        (directory / "source.json").write_text(
            json.dumps(runtime_source(mode)), encoding="utf-8"
        )
        (directory / "payload.json").write_text(
            json.dumps(runtime_payload()), encoding="utf-8"
        )
        (directory / "mappings.json").write_text("{}", encoding="utf-8")
        if mode == "publish":
            (directory / "reservations.json").write_text(
                json.dumps({"test-101": {"stage": "prepared"}}),
                encoding="utf-8",
            )

    @patch.object(publish_payloads, "load_blocked_products", return_value={})
    @patch.object(
        publish_payloads,
        "build_and_validate",
        return_value=({"attributes": []}, {"attempts": [], "warnings": []}),
    )
    def test_dry_run_does_not_write_mapping_or_journal(self, _build, _blocked):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_inputs(directory, "dry-run")
            exit_code = publish_payloads.run(self.make_args(directory, False))
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads((directory / "mappings.json").read_text()), {}
            )
            self.assertFalse((directory / "journal.json").exists())
            self.assertEqual(
                json.loads((directory / "result.json").read_text())["outcome"],
                "validated",
            )

    @patch.object(
        publish_payloads,
        "publish_meli_item",
        side_effect=[{"id": "MLC-GREY"}, {"id": "MLC-BLACK"}],
    )
    @patch.object(publish_payloads, "update_meli_item_description", return_value=True)
    @patch.object(
        publish_payloads,
        "ensure_item_active",
        side_effect=[
            {"status": "active", "sub_status": [], "warnings": []},
            {"status": "active", "sub_status": [], "warnings": []},
        ],
    )
    @patch.object(
        publish_payloads,
        "audit_persisted_attributes",
        side_effect=[
            {
                "status": "active", "sub_status": [], "warnings": [],
                "available_quantity": 2, "price": 12000,
                "seller_custom_field": "SKU-GREY",
                "attributes": [{"id": "COLOR", "value_name": "Gris"}],
                "pictures": [{"id": "p1"}], "shipping": {"mode": "me2"},
                "family_id": "FAMILY-1", "user_product_id": "UP-1",
                "_missing_expected_attributes": [],
            },
            {
                "status": "active", "sub_status": [], "warnings": [],
                "available_quantity": 3, "price": 13000,
                "seller_custom_field": "SKU-BLACK",
                "attributes": [{"id": "COLOR", "value_name": "Negro"}],
                "pictures": [{"id": "p2"}], "shipping": {"mode": "me2"},
                "family_id": "FAMILY-1", "user_product_id": "UP-2",
                "_missing_expected_attributes": [],
            },
        ],
    )
    def test_family_publication_persists_mapping_by_shopify_variant(
        self, _audit, _ensure, _description, create
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            mappings_path = directory / "mappings.json"
            mappings_path.write_text("{}", encoding="utf-8")
            reservations_path = directory / "reservations.json"
            reservations_path.write_text(
                json.dumps({"test-101": {"stage": "prepared"}}), encoding="utf-8"
            )
            items = []
            for variant_id, sku, color, stock, price, image in (
                ("v-grey", "SKU-GREY", "Gris", 2, 12000, "https://cdn.shopify.com/a.jpg"),
                ("v-black", "SKU-BLACK", "Negro", 3, 13000, "https://cdn.shopify.com/b.jpg"),
            ):
                items.append({
                    "shopify_variant_id": variant_id,
                    "shopify_inventory_item_id": "",
                    "sku": sku,
                    "images": [image],
                    "attribute_combinations": [{"id": "COLOR", "value_name": color}],
                    "payload": {
                        "available_quantity": stock,
                        "price": price,
                        "attributes": [{"id": "COLOR", "value_name": color}],
                    },
                })
            candidate = {
                "_publication_mode": "user_products_family",
                "family_name": "Cable",
                "items": items,
            }
            prepared = runtime_payload()[0]
            prepared["variations"] = [
                {"shopify_variant_id": item["shopify_variant_id"], "sku": item["sku"]}
                for item in items
            ]
            code = publish_payloads.publish_family_candidate(
                args=self.make_args(directory, True), candidate=candidate,
                payload=prepared, validation={"attempts": []},
                shopify_id="test-101", title="Cable", mappings={},
                mappings_path=mappings_path, blocked_path=directory / "blocked.json",
                journal_path=directory / "journal.json",
                reservations_path=reservations_path, result_path=directory / "result.json",
            )
            self.assertEqual(code, 0)
            mapping = json.loads(mappings_path.read_text())["test-101"]
            self.assertEqual(mapping["mode"], "user_products_family")
            self.assertEqual(mapping["family_id"], "FAMILY-1")
            self.assertEqual(mapping["variants"]["v-grey"]["meli_item_id"], "MLC-GREY")
            self.assertEqual(mapping["variants"]["v-black"]["user_product_id"], "UP-2")
            self.assertEqual(json.loads((directory / "journal.json").read_text()), {})
            self.assertEqual(json.loads(reservations_path.read_text()), {})
            self.assertEqual(create.call_count, 2)

    @patch.object(publish_payloads, "publish_meli_item", return_value={"id": "MLC-BLACK"})
    @patch.object(publish_payloads, "update_meli_item_description", return_value=True)
    @patch.object(
        publish_payloads,
        "ensure_item_active",
        return_value={"status": "active", "sub_status": [], "warnings": []},
    )
    @patch.object(
        publish_payloads,
        "audit_persisted_attributes",
        return_value={
            "id": "MLC-BLACK",
            "status": "active",
            "sub_status": [],
            "warnings": [],
            "available_quantity": 3,
            "price": 13000,
            "seller_custom_field": "SKU-BLACK",
            "attributes": [{"id": "COLOR", "value_name": "Negro"}],
            "pictures": [{"id": "p2"}],
            "shipping": {"mode": "me2", "free_shipping": True},
            "family_id": "FAMILY-1",
            "user_product_id": "UP-2",
            "_missing_expected_attributes": [],
        },
    )
    def test_family_expansion_creates_only_unmapped_sellable_variant(
        self, _audit, _ensure, _description, create
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            mappings_path = directory / "mappings.json"
            existing = {
                "mode": "user_products_family",
                "family_id": "FAMILY-1",
                "family_name": "Cable",
                "variants": {
                    "v-grey": {
                        "shopify_variant_id": "v-grey",
                        "sku": "SKU-GREY",
                        "meli_item_id": "MLC-GREY",
                        "user_product_id": "UP-1",
                    }
                },
                "unpublished_variants": {"v-black": {"reason": "not_sellable"}},
            }
            mappings = {"test-101": existing}
            mappings_path.write_text(json.dumps(mappings), encoding="utf-8")
            reservations_path = directory / "reservations.json"
            reservations_path.write_text(
                json.dumps({"test-101": {"stage": "prepared"}}), encoding="utf-8"
            )
            items = [
                {
                    "shopify_variant_id": "v-grey",
                    "shopify_inventory_item_id": "",
                    "sku": "SKU-GREY",
                    "images": ["https://cdn.shopify.com/a.jpg"],
                    "attribute_combinations": [{"id": "COLOR", "value_name": "Gris"}],
                    "payload": {"available_quantity": 2, "price": 12000, "attributes": []},
                },
                {
                    "shopify_variant_id": "v-black",
                    "shopify_inventory_item_id": "",
                    "sku": "SKU-BLACK",
                    "images": ["https://cdn.shopify.com/b.jpg"],
                    "attribute_combinations": [{"id": "COLOR", "value_name": "Negro"}],
                    "payload": {
                        "available_quantity": 3,
                        "price": 13000,
                        "attributes": [{"id": "COLOR", "value_name": "Negro"}],
                    },
                },
            ]
            prepared = runtime_payload()[0]
            prepared["variations"] = [
                {"shopify_variant_id": "v-grey", "sku": "SKU-GREY"},
                {"shopify_variant_id": "v-black", "sku": "SKU-BLACK"},
            ]
            code = publish_payloads.publish_family_candidate(
                args=self.make_args(directory, True),
                candidate={
                    "_publication_mode": "user_products_family",
                    "family_name": "Cable",
                    "items": items,
                },
                payload=prepared,
                validation={"attempts": []},
                shopify_id="test-101",
                title="Cable",
                mappings=mappings,
                mappings_path=mappings_path,
                blocked_path=directory / "blocked.json",
                journal_path=directory / "journal.json",
                reservations_path=reservations_path,
                result_path=directory / "result.json",
            )
            self.assertEqual(code, 0)
            mapping = json.loads(mappings_path.read_text())["test-101"]
            self.assertEqual(set(mapping["variants"]), {"v-grey", "v-black"})
            self.assertEqual(mapping["variants"]["v-grey"]["meli_item_id"], "MLC-GREY")
            create.assert_called_once()

    @patch.object(publish_payloads, "load_blocked_products", return_value={})
    @patch.object(
        publish_payloads,
        "build_and_validate",
        return_value=(
            {"attributes": [{"id": "BRAND", "value_name": "Zipp"}]},
            {"attempts": [], "warnings": []},
        ),
    )
    @patch.object(publish_payloads, "publish_meli_item", return_value={"id": "MLC123"})
    @patch.object(publish_payloads, "update_meli_item_description", return_value=True)
    @patch.object(
        publish_payloads,
        "ensure_item_active",
        return_value={"status": "active", "sub_status": [], "warnings": []},
    )
    @patch.object(
        publish_payloads,
        "audit_persisted_attributes",
        return_value={
            "status": "active",
            "sub_status": [],
            "warnings": [],
            "shipping": {"mode": "me2", "free_shipping": True},
            "_missing_expected_attributes": [],
            "_attributes_patched": False,
        },
    )
    def test_success_maps_only_after_final_gate(
        self, _audit, _ensure, _description, create, _build, _blocked
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_inputs(directory, "publish")
            exit_code = publish_payloads.run(self.make_args(directory, True))
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads((directory / "mappings.json").read_text()),
                {"test-101": "MLC123"},
            )
            self.assertEqual(json.loads((directory / "journal.json").read_text()), {})
            self.assertEqual(
                json.loads((directory / "reservations.json").read_text()), {}
            )
            quality = json.loads((directory / "quality.json").read_text())["test-101"]
            self.assertIn("clean_active_gate", quality["achieved"])
            self.assertIn("free_shipping_live", quality["achieved"])
            create.assert_called_once()

    @patch.object(publish_payloads, "load_blocked_products", return_value={})
    @patch.object(
        publish_payloads,
        "build_and_validate",
        return_value=({"attributes": []}, {"attempts": [], "warnings": []}),
    )
    @patch.object(publish_payloads, "publish_meli_item", return_value={"id": "MLC123"})
    @patch.object(
        publish_payloads,
        "update_meli_item_description",
        side_effect=requests.Timeout("timeout"),
    )
    def test_failure_after_creation_preserves_journal(
        self, _description, _create, _build, _blocked
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_inputs(directory, "publish")
            exit_code = publish_payloads.run(self.make_args(directory, True))
            self.assertEqual(exit_code, 4)
            self.assertEqual(
                json.loads((directory / "journal.json").read_text())["test-101"][
                    "meli_id"
                ],
                "MLC123",
            )
            self.assertEqual(
                json.loads((directory / "mappings.json").read_text()), {}
            )
            self.assertIn(
                "test-101",
                json.loads((directory / "reservations.json").read_text()),
            )
            result = json.loads((directory / "result.json").read_text())
            self.assertEqual(result["outcome"], "failed_retryable")
            self.assertTrue(result["resume_same_item"])

    @patch.object(publish_payloads, "load_blocked_products", return_value={})
    @patch.object(
        publish_payloads,
        "build_and_validate",
        return_value=({"attributes": []}, {"attempts": [], "warnings": []}),
    )
    def test_live_publish_requires_publish_mode_source(self, _build, _blocked):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_inputs(directory, "dry-run")
            exit_code = publish_payloads.run(self.make_args(directory, True))
            self.assertEqual(exit_code, 2)
            self.assertFalse((directory / "journal.json").exists())
            self.assertIn(
                "requires source.json prepared with --mode publish",
                json.loads((directory / "result.json").read_text())["reason"],
            )

    @patch.object(publish_payloads, "load_blocked_products", return_value={})
    @patch.object(
        publish_payloads,
        "build_and_validate",
        return_value=({"attributes": []}, {"attempts": [], "warnings": []}),
    )
    @patch.object(publish_payloads, "publish_meli_item")
    @patch.object(
        publish_payloads,
        "fetch_item",
        return_value={"status": "active", "sub_status": [], "warnings": []},
    )
    @patch.object(publish_payloads, "update_meli_item_description", return_value=True)
    @patch.object(
        publish_payloads,
        "ensure_item_active",
        return_value={"status": "active", "sub_status": [], "warnings": []},
    )
    @patch.object(
        publish_payloads,
        "audit_persisted_attributes",
        return_value={
            "status": "active",
            "sub_status": [],
            "warnings": [],
            "_missing_expected_attributes": [],
            "_attributes_patched": False,
        },
    )
    def test_journaled_item_is_resumed_without_creating_replacement(
        self,
        _audit,
        _ensure,
        _description,
        _fetch,
        create,
        _build,
        _blocked,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_inputs(directory, "publish")
            (directory / "journal.json").write_text(
                json.dumps(
                    {
                        "test-101": {
                            "meli_id": "MLC_EXISTING",
                            "stage": "created",
                        }
                    }
                ),
                encoding="utf-8",
            )
            exit_code = publish_payloads.run(self.make_args(directory, True))
            self.assertEqual(exit_code, 0)
            create.assert_not_called()
            self.assertEqual(
                json.loads((directory / "mappings.json").read_text()),
                {"test-101": "MLC_EXISTING"},
            )
            self.assertEqual(
                json.loads((directory / "reservations.json").read_text()), {}
            )


if __name__ == "__main__":
    unittest.main()
