from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "meli.py"
if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv_stub
SPEC = importlib.util.spec_from_file_location("one_by_one_meli", MODULE_PATH)
assert SPEC and SPEC.loader
meli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = meli
SPEC.loader.exec_module(meli)


def make_product(
    quantities: tuple[int, ...] = (5,),
    prices: tuple[str, ...] | None = None,
) -> dict:
    prices = prices or tuple("1000" for _ in quantities)
    variants = []
    for index, (quantity, price) in enumerate(zip(quantities, prices), start=1):
        variants.append(
            {
                "id": f"gid://shopify/ProductVariant/{index}",
                "legacy_id": str(index),
                "title": f"Variant {index}",
                "sku": f"SKU-{index}",
                "barcode": "",
                "price": price,
                "inventory_quantity": quantity,
                "inventory_tracked": True,
                "selected_options": [{"name": "Color", "value": f"Color {index}"}],
                "image": None,
            }
        )
    return {
        "id": "gid://shopify/Product/100",
        "legacy_id": "100",
        "title": "Producto de prueba",
        "description_html": "<p>Descripción Shopify</p>",
        "vendor": "Vendor",
        "product_type": "Accesorio",
        "status": "ACTIVE",
        "tags": [],
        "options": [{"name": "Color", "values": [f"Color {i}" for i in range(1, len(variants) + 1)]}],
        "images": [
            {"url": "https://cdn.shopify.com/image-1.jpg", "alt_text": "Frente"},
            {"url": "https://cdn.shopify.com/image-2.jpg", "alt_text": "Detalle"},
        ],
        "variants": variants,
    }


def make_source(product: dict, model: str = "user_products_family") -> dict:
    targets = meli.target_variant_ids(product, None, model)
    return {
        "dry_run": False,
        "publication_model": model,
        "target_variant_ids": targets,
        "existing_mapping": None,
        "product_fingerprint": meli.source_fingerprint(product),
        "product": product,
    }


def make_payload(product: dict, model: str = "user_products_family") -> dict:
    payload = {
        "shopify_id": product["id"],
        "category_id": "MLC123",
        "description": "Descripción clara del producto.",
        "brand": "Genérica",
        "model": "No aplica",
        "pictures": [image["url"] for image in product["images"]],
        "attributes": [],
        "variant_attributes": {},
        "variant_pictures": {},
    }
    payload["family_name" if model == "user_products_family" else "title"] = (
        "Producto de prueba"
    )
    if len(product["variants"]) > 1:
        payload["variant_attributes"] = {
            variant["id"]: [{"id": "COLOR", "value_name": f"Color {index}"}]
            for index, variant in enumerate(product["variants"], start=1)
        }
    return payload


def make_settings(root: Path) -> meli.Settings:
    return meli.Settings(
        shopify_url="https://example.myshopify.com",
        shopify_token="shopify-token",
        shopify_version="2026-07",
        meli_app_id="app",
        meli_client_secret="secret",
        meli_seller_id="77",
        meli_tokens_file=root / "tokens.json",
        stock_worker_lock_file=root / "stock-worker-lock.db",
        meli_site_id="MLC",
        listing_type_id="gold_special",
    )


class EnvironmentTests(unittest.TestCase):
    def test_shared_environment_file_can_live_outside_the_worktree(self):
        shared = "/git/prod/stock-sync/.env"
        with (
            patch.dict(
                meli.os.environ,
                {"PRODUCT_SYNC_SHARED_ENV_FILE": shared},
                clear=True,
            ),
            patch.object(meli, "load_dotenv") as load_dotenv,
        ):
            meli.load_environment()

        self.assertEqual(load_dotenv.call_count, 2)
        self.assertEqual(load_dotenv.call_args_list[0].args[0], meli.SKILL_DIR / ".env")
        self.assertEqual(load_dotenv.call_args_list[1].args[0], Path(shared))


class FakeShopify:
    def __init__(self, product: dict):
        self.product = product

    def get_product(self, product_id: str) -> dict | None:
        return self.product if product_id == self.product["id"] else None

    def active_product_refs(self) -> list[dict]:
        return [
            {"id": self.product["id"], "title": self.product["title"], "status": "ACTIVE"}
        ]

    def all_product_refs(self) -> list[dict]:
        return self.active_product_refs()

    def all_variant_refs(self) -> list[dict]:
        return [
            {
                "id": variant["id"],
                "sku": variant["sku"],
                "product_id": self.product["id"],
                "product_status": self.product["status"],
            }
            for variant in self.product["variants"]
        ]


class FakeMeli:
    def __init__(self, valid: bool = True):
        self.valid = valid
        self.events: list[tuple] = []
        self.items: dict[str, dict] = {}
        self.descriptions: dict[str, str] = {}
        self.existing_ids: list[str] = []

    def verify_seller(self) -> dict:
        return {"id": 77, "tags": ["user_product_seller"]}

    def discover_categories(self, query: str) -> list[dict]:
        return []

    def category_attributes(self, category_id: str) -> list[dict]:
        return []

    def validate_item(self, body: dict) -> dict:
        self.events.append(("validate", body.get("seller_custom_field")))
        return {
            "valid": self.valid,
            "status": 204 if self.valid else 400,
            "warnings": [],
            "errors": [] if self.valid else [{"type": "error"}],
        }

    def search_exact_sku(self, sku: str) -> list[str]:
        return list(self.existing_ids)

    def create_item(self, body: dict) -> dict:
        self.events.append(("create", body.get("seller_custom_field")))
        item_id = f"MLC{len(self.items) + 1}"
        item = {
            **body,
            "id": item_id,
            "seller_id": 77,
            "status": "active",
            "sub_status": [],
            "family_id": "FAMILY-1",
            "user_product_id": f"UP-{len(self.items) + 1}",
        }
        self.items[item_id] = item
        return item

    def get_item(self, item_id: str) -> dict | None:
        return self.items.get(item_id)

    def get_description(self, item_id: str) -> str | None:
        return self.descriptions.get(item_id)

    def set_description(self, item_id: str, description: str) -> None:
        self.events.append(("description", item_id))
        self.descriptions[item_id] = description

    def set_status(self, item_id: str, status: str) -> None:
        self.events.append(("status", item_id, status))
        self.items[item_id]["status"] = status


class SelectionAndPayloadTests(unittest.TestCase):
    def test_selection_skips_product_with_no_sellable_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = make_product((0, 0))
            empty["id"] = "gid://shopify/Product/1"
            sellable = make_product((3,))
            sellable["id"] = "gid://shopify/Product/2"
            sellable["variants"][0]["id"] = "gid://shopify/ProductVariant/3"
            sellable["variants"][0]["sku"] = "SKU-3"

            class NextShopify:
                def active_product_refs(self):
                    return [
                        {"id": empty["id"], "title": empty["title"], "status": "ACTIVE"},
                        {"id": sellable["id"], "title": sellable["title"], "status": "ACTIVE"},
                    ]

                def get_product(self, product_id):
                    return {empty["id"]: empty, sellable["id"]: sellable}[product_id]

                def all_variant_refs(self):
                    return [
                        {
                            "id": variant["id"],
                            "sku": variant["sku"],
                            "product_id": product["id"],
                            "product_status": product["status"],
                        }
                        for product in (empty, sellable)
                        for variant in product["variants"]
                    ]

            class NextMeli:
                def verify_seller(self):
                    return {"id": 77, "tags": []}

                def discover_categories(self, query):
                    return []

                def category_attributes(self, category_id):
                    raise AssertionError("no category was returned")

            state_path = root / "state.json"
            state = meli.default_state()
            state["bootstrap"] = {"completed_at": "2026-01-01T00:00:00Z"}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            output_path = root / "source.json"
            args = argparse.Namespace(
                state=str(state_path),
                lock=str(root / "publisher.lock"),
                output=str(output_path),
                dry_run=False,
            )
            with (
                patch.object(meli, "ShopifyClient", return_value=NextShopify()),
                patch.object(meli, "MeliClient", return_value=NextMeli()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(meli.command_next(args, make_settings(root)), 0)
            source = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(source["product"]["id"], sellable["id"])
            self.assertEqual(source["publication_model"], "legacy_variations")
            self.assertEqual(source["target_variant_ids"], [sellable["variants"][0]["id"]])

    def test_user_products_publish_only_sellable_unmapped_variants(self):
        product = make_product((4, 0, 2))
        mapping = {
            "publication_model": "user_products_family",
            "variants": {product["variants"][0]["id"]: {"meli_item_id": "MLC1"}},
        }
        self.assertEqual(
            meli.target_variant_ids(product, mapping, "user_products_family"),
            [product["variants"][2]["id"]],
        )

    def test_user_product_payload_owns_commercial_fields(self):
        product = make_product((7,))
        candidate = meli.build_candidates(
            make_source(product), make_payload(product), "gold_special"
        )[0]
        body = candidate["body"]
        self.assertEqual(body["seller_custom_field"], "SKU-1")
        self.assertEqual(body["price"], 1000)
        self.assertEqual(body["available_quantity"], 7)
        self.assertEqual(meli.attribute_value(body["attributes"], "SELLER_SKU"), "SKU-1")

    def test_legacy_payload_keeps_zero_stock_variant(self):
        product = make_product((5, 0))
        candidate = meli.build_candidates(
            make_source(product, "legacy_variations"),
            make_payload(product, "legacy_variations"),
            "gold_special",
        )[0]
        self.assertEqual(
            [variation["available_quantity"] for variation in candidate["body"]["variations"]],
            [5, 0],
        )

    def test_missing_family_name_is_rejected(self):
        product = make_product()
        payload = make_payload(product)
        payload.pop("family_name")
        with self.assertRaisesRegex(meli.ReviewRequired, "family_name is required"):
            meli.build_candidates(make_source(product), payload, "gold_special")

    def test_store_or_contact_content_is_rejected(self):
        product = make_product()
        payload = make_payload(product)
        payload["description"] = "Visita zipp.cl para más información"
        with self.assertRaisesRegex(meli.ReviewRequired, "store or contact"):
            meli.build_candidates(make_source(product), payload, "gold_special")

    def test_live_item_requires_both_exact_sku_representations(self):
        product = make_product()
        candidate = meli.build_candidates(
            make_source(product), make_payload(product), "gold_special"
        )[0]
        live = {
            **candidate["body"],
            "seller_id": 77,
            "status": "active",
            "sub_status": [],
            "family_id": "FAMILY-1",
            "user_product_id": "UP-1",
        }
        live["attributes"] = [
            attribute
            for attribute in live["attributes"]
            if attribute.get("id") != "SELLER_SKU"
        ]
        self.assertIn(
            "SELLER_SKU differs for SKU SKU-1",
            meli.item_match_errors(candidate, live, "77", require_active=True),
        )

    def test_duplicate_live_variation_sku_is_rejected(self):
        product = make_product((5, 0))
        candidate = meli.build_candidates(
            make_source(product, "legacy_variations"),
            make_payload(product, "legacy_variations"),
            "gold_special",
        )[0]
        live = {
            **candidate["body"],
            "seller_id": 77,
            "status": "active",
            "sub_status": [],
            "variations": [
                *candidate["body"]["variations"],
                dict(candidate["body"]["variations"][0]),
            ],
        }
        self.assertIn(
            "SKU SKU-1 occurs more than once",
            meli.item_match_errors(candidate, live, "77", require_active=True),
        )

    def test_extra_skuless_live_variation_is_rejected(self):
        product = make_product((5, 0))
        candidate = meli.build_candidates(
            make_source(product, "legacy_variations"),
            make_payload(product, "legacy_variations"),
            "gold_special",
        )[0]
        live = {
            **candidate["body"],
            "seller_id": 77,
            "status": "active",
            "sub_status": [],
            "variations": [
                *candidate["body"]["variations"],
                {
                    "available_quantity": 2,
                    "price": 1000,
                    "attribute_combinations": [
                        {"id": "COLOR", "value_name": "Invisible"}
                    ],
                    "attributes": [],
                },
            ],
        }
        errors = meli.item_match_errors(candidate, live, "77", require_active=True)
        self.assertIn("variation count differs", errors)
        self.assertIn("variation 2 is missing an exact SKU field", errors)


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.product = make_product((5, 2))
        self.source = make_source(self.product)
        self.payload = make_payload(self.product)
        self.state_path = self.root / "state.json"
        self.source_path = self.root / "source.json"
        self.payload_path = self.root / "payload.json"
        self.result_path = self.root / "result.json"
        self.lock_path = self.root / "publisher.lock"
        self.settings = make_settings(self.root)
        self.state = meli.default_state()
        self.state["bootstrap"] = {"completed_at": "2026-01-01T00:00:00Z"}
        self.state["pending"] = {
            "shopify_id": self.product["id"],
            "publication_model": "user_products_family",
            "target_variant_ids": self.source["target_variant_ids"],
            "items": {},
            "created_keys": [],
        }
        for path, value in (
            (self.state_path, self.state),
            (self.source_path, self.source),
            (self.payload_path, self.payload),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            source=str(self.source_path),
            payload=str(self.payload_path),
            result=str(self.result_path),
            dry_run=False,
            poll_attempts=1,
            poll_delay=0,
        )

    def run_publish(
        self, fake_meli: FakeMeli, fake_shopify: FakeShopify | None = None
    ) -> int:
        with (
            patch.object(
                meli,
                "ShopifyClient",
                return_value=fake_shopify or FakeShopify(self.product),
            ),
            patch.object(meli, "MeliClient", return_value=fake_meli),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return meli.command_publish(self.args(), self.settings)

    def test_validates_all_members_before_creating_and_maps_one_shopify_product(self):
        fake = FakeMeli()
        self.assertEqual(self.run_publish(fake), 0)
        event_names = [event[0] for event in fake.events]
        first_create = event_names.index("create")
        self.assertEqual(event_names[:first_create], ["validate", "validate"])
        self.assertEqual(event_names.count("create"), 2)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["pending"])
        mapping = state["mappings"][self.product["id"]]
        self.assertEqual(len(mapping["variants"]), 2)
        self.assertEqual(mapping["family_id"], "FAMILY-1")

    def test_validation_failure_creates_nothing_and_keeps_pending(self):
        fake = FakeMeli(valid=False)
        self.assertEqual(self.run_publish(fake), 2)
        self.assertNotIn("create", [event[0] for event in fake.events])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(state["pending"])

    def test_uncertain_create_never_attempts_a_duplicate(self):
        self.state["pending"]["creating_key"] = self.product["variants"][0]["id"]
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        fake = FakeMeli()
        with self.assertRaisesRegex(meli.SyncError, "refusing a duplicate"):
            self.run_publish(fake)
        self.assertNotIn("create", [event[0] for event in fake.events])

    def test_definite_create_rejection_clears_uncertain_marker(self):
        class RejectingMeli(FakeMeli):
            def create_item(self, body: dict) -> dict:
                self.events.append(("create_rejected", body.get("seller_custom_field")))
                raise meli.CreateRejected("definite HTTP 400")

        fake = RejectingMeli()
        with self.assertRaises(meli.CreateRejected):
            self.run_publish(fake)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("creating_key", state["pending"])
        self.assertEqual(state["pending"]["stage"], "validated")

    def test_scheduled_next_preserves_uncertain_create_before_publish(self):
        creating_key = self.product["variants"][0]["id"]
        self.state["pending"]["creating_key"] = creating_key
        self.state["pending"]["stage"] = "creating"
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        fake = FakeMeli()
        next_args = argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            output=str(self.source_path),
            dry_run=False,
        )
        with (
            patch.object(meli, "ShopifyClient", return_value=FakeShopify(self.product)),
            patch.object(meli, "MeliClient", return_value=fake),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(meli.command_next(next_args, self.settings), 0)
        refreshed_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed_state["pending"]["creating_key"], creating_key)
        self.assertEqual(refreshed_state["pending"]["stage"], "creating")
        with self.assertRaisesRegex(meli.SyncError, "refusing a duplicate"):
            self.run_publish(fake)
        self.assertNotIn("create", [event[0] for event in fake.events])

    def test_next_marks_existing_item_as_payload_locked_resume(self):
        first_key = self.product["variants"][0]["id"]
        self.state["pending"]["items"] = {first_key: "MLC123"}
        self.state["pending"]["created_keys"] = [first_key]
        self.state["pending"]["stage"] = "created"
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        fake = FakeMeli()
        next_args = argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            output=str(self.source_path),
            dry_run=False,
        )
        with (
            patch.object(meli, "ShopifyClient", return_value=FakeShopify(self.product)),
            patch.object(meli, "MeliClient", return_value=fake),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(meli.command_next(next_args, self.settings), 0)

        source = json.loads(self.source_path.read_text(encoding="utf-8"))
        self.assertEqual(
            source["resume"],
            {
                "resumed": True,
                "stage": "created",
                "meli_item_ids": ["MLC123"],
                "reuse_existing_payload": True,
            },
        )

    def test_matching_existing_items_are_adopted_without_mutation(self):
        fake = FakeMeli()
        candidates = meli.build_candidates(self.source, self.payload, "gold_special")
        for index, candidate in enumerate(candidates, start=1):
            item_id = f"MLC{index}"
            fake.items[item_id] = {
                **candidate["body"],
                "id": item_id,
                "seller_id": 77,
                "status": "active",
                "sub_status": [],
                "family_id": "FAMILY-1",
                "user_product_id": f"UP-{index}",
            }
            fake.descriptions[item_id] = candidate["description"]

        def exact_ids(sku: str) -> list[str]:
            return [
                item_id
                for item_id, item in fake.items.items()
                if item.get("seller_custom_field") == sku
            ]

        fake.search_exact_sku = exact_ids
        self.assertEqual(self.run_publish(fake), 0)
        mutations = [event for event in fake.events if event[0] in {"create", "status", "description"}]
        self.assertEqual(mutations, [])

    def test_publish_rechecks_current_shopify_sku_ownership(self):
        class DuplicateShopify(FakeShopify):
            def all_variant_refs(self):
                return [
                    *super().all_variant_refs(),
                    {
                        "id": "gid://shopify/ProductVariant/999",
                        "sku": "SKU-1",
                        "product_id": "gid://shopify/Product/999",
                        "product_status": "ACTIVE",
                    },
                ]

        fake = FakeMeli()
        with self.assertRaisesRegex(meli.ValidationFailed, "SKU ownership"):
            self.run_publish(fake, DuplicateShopify(self.product))
        self.assertEqual(fake.events, [])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "state.json"
        self.result_path = self.root / "bootstrap.json"
        self.lock_path = self.root / "publisher.lock"
        self.settings = make_settings(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def args(self, apply: bool) -> argparse.Namespace:
        return argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            result=str(self.result_path),
            apply=apply,
        )

    @staticmethod
    def user_product_item(
        item_id: str,
        sku: str,
        user_product_id: str,
        *,
        seller_custom_field: bool = True,
    ) -> dict:
        return {
            "id": item_id,
            "seller_id": 77,
            "site_id": "MLC",
            "status": "active",
            "seller_custom_field": sku if seller_custom_field else "",
            "attributes": (
                []
                if seller_custom_field
                else [{"id": "SELLER_SKU", "value_name": sku}]
            ),
            "variations": [],
            "family_id": "FAMILY-1",
            "family_name": "Producto de prueba",
            "user_product_id": user_product_id,
        }

    def run_bootstrap(
        self, product: dict, items: list[dict], *, apply: bool
    ) -> tuple[int, list[str]]:
        class BootstrapShopify(FakeShopify):
            pass

        class BootstrapMeli:
            def __init__(self):
                self.mutations: list[str] = []

            def verify_seller(self):
                return {"id": 77, "tags": ["user_product_seller"]}

            def all_seller_item_ids(self):
                return [item["id"] for item in items]

            def get_item(self, item_id):
                return next(item for item in items if item["id"] == item_id)

            def create_item(self, body):
                self.mutations.append("create")
                raise AssertionError("bootstrap must not create")

            def set_status(self, item_id, status):
                self.mutations.append("status")
                raise AssertionError("bootstrap must not update")

        fake_meli = BootstrapMeli()
        with (
            patch.object(meli, "ShopifyClient", return_value=BootstrapShopify(product)),
            patch.object(meli, "MeliClient", return_value=fake_meli),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = meli.command_bootstrap(self.args(apply), self.settings)
        return result, fake_meli.mutations

    def test_live_bootstrap_maps_user_products_and_writes_no_external_data(self):
        product = make_product((5, 0))
        items = [
            self.user_product_item("MLC1", "SKU-1", "UP-1"),
            self.user_product_item(
                "MLC2", "SKU-2", "UP-2", seller_custom_field=False
            ),
        ]
        result, mutations = self.run_bootstrap(product, items, apply=True)
        self.assertEqual(result, 0)
        self.assertEqual(mutations, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        mapping = state["mappings"][product["id"]]
        self.assertEqual(mapping["publication_model"], "user_products_family")
        self.assertEqual(mapping["family_id"], "FAMILY-1")
        self.assertEqual(len(mapping["variants"]), 2)
        self.assertEqual(state["bootstrap"]["mappings_added"], 1)

    def test_preview_does_not_write_state(self):
        product = make_product()
        items = [self.user_product_item("MLC1", "SKU-1", "UP-1")]
        result, mutations = self.run_bootstrap(product, items, apply=False)
        self.assertEqual(result, 0)
        self.assertEqual(mutations, [])
        self.assertFalse(self.state_path.exists())
        report = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "bootstrap_preview")
        self.assertFalse(report["state_written"])

    def test_ambiguous_meli_sku_is_reported_and_not_mapped(self):
        product = make_product()
        items = [
            self.user_product_item("MLC1", "SKU-1", "UP-1"),
            self.user_product_item("MLC2", "SKU-1", "UP-2"),
        ]
        self.run_bootstrap(product, items, apply=True)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mappings"], {})
        self.assertEqual(state["bootstrap"]["held_product_ids"], [product["id"]])
        report = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(report["ambiguous_skus"][0]["sku"], "SKU-1")
        self.assertEqual(report["issues"][0]["type"], "sku_match_ambiguous")

    def test_partially_unambiguous_held_product_is_not_mapped(self):
        product = make_product((5, 2))
        items = [
            self.user_product_item("MLC1", "SKU-1", "UP-1"),
            self.user_product_item("MLC2", "SKU-2", "UP-2"),
            self.user_product_item("MLC3", "SKU-2", "UP-3"),
        ]
        self.run_bootstrap(product, items, apply=True)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mappings"], {})
        self.assertEqual(state["bootstrap"]["held_product_ids"], [product["id"]])

    def test_conflicting_meli_sku_fields_are_not_mapped(self):
        product = make_product()
        item = self.user_product_item("MLC1", "SKU-1", "UP-1")
        item["attributes"] = [{"id": "SELLER_SKU", "value_name": "OTHER"}]
        self.run_bootstrap(product, [item], apply=True)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mappings"], {})
        self.assertEqual(state["bootstrap"]["held_product_ids"], [product["id"]])
        report = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(report["issues"][0]["type"], "meli_sku_fields_conflict")

    def test_current_skuless_meli_item_prevents_bootstrap_application(self):
        product = make_product()
        item = self.user_product_item("MLC1", "", "UP-1")
        result, mutations = self.run_bootstrap(product, [item], apply=True)
        self.assertEqual(result, 2)
        self.assertEqual(mutations, [])
        self.assertFalse(self.state_path.exists())
        report = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "bootstrap_not_applied")
        self.assertEqual(report["application_blocking_item_ids"], ["MLC1"])

    def test_live_bootstrap_maps_complete_legacy_variations(self):
        product = make_product((5, 0))
        item = {
            "id": "MLC1",
            "seller_id": 77,
            "site_id": "MLC",
            "status": "paused",
            "variations": [
                {
                    "id": 10,
                    "seller_custom_field": variant["sku"],
                    "attributes": [],
                }
                for variant in product["variants"]
            ],
        }
        self.run_bootstrap(product, [item], apply=True)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        mapping = state["mappings"][product["id"]]
        self.assertEqual(mapping["publication_model"], "legacy_variations")
        self.assertEqual(len(mapping["variants"]), 2)

    def test_single_legacy_item_can_have_user_product_id_without_family(self):
        product = make_product()
        item = {
            "id": "MLC1",
            "seller_id": 77,
            "site_id": "MLC",
            "status": "active",
            "seller_custom_field": "SKU-1",
            "attributes": [],
            "variations": [],
            "family_id": None,
            "family_name": None,
            "user_product_id": "MLCU-LEGACY-1",
        }
        self.run_bootstrap(product, [item], apply=True)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        mapping = state["mappings"][product["id"]]
        self.assertEqual(mapping["publication_model"], "legacy_variations")
        variant = next(iter(mapping["variants"].values()))
        self.assertEqual(variant["user_product_id"], "MLCU-LEGACY-1")

    def test_production_next_requires_applied_bootstrap(self):
        product = make_product()
        args = argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            output=str(self.root / "source.json"),
            dry_run=False,
        )
        with self.assertRaisesRegex(meli.ReviewRequired, "bootstrap must be applied"):
            meli.command_next(args, self.settings)

    def test_next_skips_product_mapped_by_bootstrap(self):
        mapped = make_product()
        mapped["id"] = "gid://shopify/Product/1"
        unsynced = make_product()
        unsynced["id"] = "gid://shopify/Product/2"
        unsynced["variants"][0]["id"] = "gid://shopify/ProductVariant/2"
        unsynced["variants"][0]["sku"] = "SKU-2"
        state = meli.default_state()
        state["bootstrap"] = {"completed_at": "2026-01-01T00:00:00Z"}
        state["mappings"][mapped["id"]] = {
            "publication_model": "user_products_family",
            "family_name": "Producto de prueba",
            "family_id": "FAMILY-1",
            "variants": {
                mapped["variants"][0]["id"]: {
                    "sku": "SKU-1",
                    "meli_item_id": "MLC1",
                    "meli_variation_id": None,
                    "user_product_id": "UP-1",
                }
            },
            "deferred_variant_ids": [],
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        class NextShopify:
            def active_product_refs(self):
                return [
                    {"id": mapped["id"], "title": mapped["title"], "status": "ACTIVE"},
                    {"id": unsynced["id"], "title": unsynced["title"], "status": "ACTIVE"},
                ]

            def get_product(self, product_id):
                return {mapped["id"]: mapped, unsynced["id"]: unsynced}[product_id]

            def all_variant_refs(self):
                return [
                    {
                        "id": variant["id"],
                        "sku": variant["sku"],
                        "product_id": product["id"],
                        "product_status": product["status"],
                    }
                    for product in (mapped, unsynced)
                    for variant in product["variants"]
                ]

        class NextMeli:
            def verify_seller(self):
                return {"id": 77, "tags": ["user_product_seller"]}

            def discover_categories(self, query):
                return []

            def category_attributes(self, category_id):
                raise AssertionError("no category was returned")

        output_path = self.root / "source.json"
        args = argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            output=str(output_path),
            dry_run=False,
        )
        with (
            patch.object(meli, "ShopifyClient", return_value=NextShopify()),
            patch.object(meli, "MeliClient", return_value=NextMeli()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(meli.command_next(args, self.settings), 0)
        source = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(source["product"]["id"], unsynced["id"])

    def test_next_skips_product_held_by_bootstrap_ambiguity(self):
        held = make_product()
        held["id"] = "gid://shopify/Product/1"
        unsynced = make_product()
        unsynced["id"] = "gid://shopify/Product/2"
        unsynced["variants"][0]["id"] = "gid://shopify/ProductVariant/2"
        unsynced["variants"][0]["sku"] = "SKU-2"
        state = meli.default_state()
        state["bootstrap"] = {
            "completed_at": "2026-01-01T00:00:00Z",
            "held_product_ids": [held["id"]],
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        class NextShopify:
            def active_product_refs(self):
                return [
                    {"id": held["id"], "title": held["title"], "status": "ACTIVE"},
                    {"id": unsynced["id"], "title": unsynced["title"], "status": "ACTIVE"},
                ]

            def get_product(self, product_id):
                if product_id == held["id"]:
                    raise AssertionError("held product must not be selected")
                return unsynced

            def all_variant_refs(self):
                return [
                    {
                        "id": variant["id"],
                        "sku": variant["sku"],
                        "product_id": product["id"],
                        "product_status": product["status"],
                    }
                    for product in (held, unsynced)
                    for variant in product["variants"]
                ]

        class NextMeli:
            def verify_seller(self):
                return {"id": 77, "tags": ["user_product_seller"]}

            def discover_categories(self, query):
                return []

            def category_attributes(self, category_id):
                raise AssertionError("no category was returned")

        output_path = self.root / "source.json"
        args = argparse.Namespace(
            state=str(self.state_path),
            lock=str(self.lock_path),
            output=str(output_path),
            dry_run=False,
        )
        with (
            patch.object(meli, "ShopifyClient", return_value=NextShopify()),
            patch.object(meli, "MeliClient", return_value=NextMeli()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(meli.command_next(args, self.settings), 0)
        source = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(source["product"]["id"], unsynced["id"])

    def test_scan_paginates_until_empty_results(self):
        client = object.__new__(meli.MeliClient)
        client.settings = self.settings
        calls: list[dict] = []
        pages = [
            {"results": ["MLC1", "MLC2"], "scroll_id": "SCROLL"},
            {"results": ["MLC3"], "scroll_id": "SCROLL"},
            {"results": None, "scroll_id": "SCROLL"},
        ]

        def request(method, path, **kwargs):
            calls.append(kwargs["params"])
            return pages.pop(0)

        client.request = request
        self.assertEqual(client.all_seller_item_ids(), ["MLC1", "MLC2", "MLC3"])
        self.assertNotIn("scroll_id", calls[0])
        self.assertEqual(calls[1]["scroll_id"], "SCROLL")

    def test_exact_search_keeps_item_with_conflicting_sku_fields(self):
        client = object.__new__(meli.MeliClient)
        client.settings = self.settings
        item = {
            "id": "MLC1",
            "seller_custom_field": "SKU-A",
            "attributes": [{"id": "SELLER_SKU", "value_name": "SKU-B"}],
            "variations": [],
        }

        def request(method, path, **kwargs):
            field = "sku" if "sku" in kwargs["params"] else "seller_sku"
            return {
                "results": ["MLC1"] if field == "seller_sku" else [],
                "paging": {"total": 1 if field == "seller_sku" else 0},
            }

        client.request = request
        client.get_item = lambda item_id: item
        self.assertEqual(client.search_exact_sku("SKU-B"), ["MLC1"])

    def test_exact_search_checks_parent_sku_on_legacy_variation_item(self):
        client = object.__new__(meli.MeliClient)
        client.settings = self.settings
        item = {
            "id": "MLC1",
            "seller_custom_field": "PARENT-SKU",
            "attributes": [],
            "variations": [
                {
                    "id": 10,
                    "seller_custom_field": "CHILD-SKU",
                    "attributes": [],
                }
            ],
        }

        def request(method, path, **kwargs):
            field = "sku" if "sku" in kwargs["params"] else "seller_sku"
            return {
                "results": ["MLC1"] if field == "sku" else [],
                "paging": {"total": 1 if field == "sku" else 0},
            }

        client.request = request
        client.get_item = lambda item_id: item
        self.assertEqual(client.search_exact_sku("PARENT-SKU"), ["MLC1"])

    def test_duplicate_shopify_sku_without_meli_match_holds_both_products(self):
        first = make_product()
        first["id"] = "gid://shopify/Product/1"
        first["variants"][0]["id"] = "gid://shopify/ProductVariant/1"
        second = make_product()
        second["id"] = "gid://shopify/Product/2"
        second["variants"][0]["id"] = "gid://shopify/ProductVariant/2"
        discovery = meli.discover_live_mappings(
            [first, second], [], "77", "MLC", "2026-01-01T00:00:00Z"
        )
        self.assertEqual(
            discovery["held_product_ids"], [first["id"], second["id"]]
        )
        self.assertEqual(discovery["issues"][0]["type"], "sku_match_ambiguous")

    def test_current_sku_snapshot_detects_reuse_and_mapped_sku_drift(self):
        mapping = {
            "gid://shopify/Product/1": {
                "publication_model": "legacy_variations",
                "variants": {
                    "gid://shopify/ProductVariant/1": {
                        "sku": "MAPPED-SKU",
                        "meli_item_id": "MLC1",
                    }
                },
            }
        }
        refs = [
            {
                "id": "gid://shopify/ProductVariant/1",
                "sku": "CHANGED-SKU",
                "product_id": "gid://shopify/Product/1",
                "product_status": "ACTIVE",
            },
            {
                "id": "gid://shopify/ProductVariant/2",
                "sku": "MAPPED-SKU",
                "product_id": "gid://shopify/Product/2",
                "product_status": "ACTIVE",
            },
        ]
        holds = meli.current_sku_holds(refs, mapping)
        self.assertEqual(
            set(holds),
            {"gid://shopify/Product/1", "gid://shopify/Product/2"},
        )


if __name__ == "__main__":
    unittest.main()
