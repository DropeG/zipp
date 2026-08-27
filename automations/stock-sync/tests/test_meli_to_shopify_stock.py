import importlib.util
import sqlite3
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_meli_to_shopify_stock.py"


def load_module():
    spec = importlib.util.spec_from_file_location("process_meli_to_shopify_stock", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def paid_order(order_id="2000018107143682", sku="SOP-BAS-58", quantity=1, item_id="MLC4243842760"):
    return {
        "id": order_id,
        "status": "paid",
        "seller": {"id": 1604295292},
        "order_items": [
            {
                "quantity": quantity,
                "item": {
                    "id": item_id,
                    "title": "Soporte Plegable Para Computador Gris",
                    "seller_custom_field": sku,
                },
            }
        ],
    }


def shopify_variant(sku="SOP-BAS-58", variant_id=41798285164679, inventory_item_id=43895215915143):
    return {
        "id": variant_id,
        "title": "Default Title",
        "sku": sku,
        "inventory_item_id": inventory_item_id,
        "inventory_management": "shopify",
        "product_id": 7859085082759,
        "product_title": "Soporte plegable para computador",
    }


class MeliToShopifyStockTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.mod.ensure_schema(self.conn)

    def rows(self, table):
        return self.conn.execute(f"SELECT * FROM {table}").fetchall()

    def insert_pending_task(self, sku="SOP-BAS-58", quantity=1):
        task_id = f"meli:order:1:item:{sku}"
        self.conn.execute(
            """
            INSERT INTO stock_tasks (
              task_id, source, order_id, order_name, line_item_id, sku,
              shopify_variant_id, quantity_sold, status, human_note,
              line_item_json, created_at, updated_at
            ) VALUES (?, 'meli', 'order', 'Meli #order', '1', ?, NULL, ?, 'pending', NULL, '{}', 'now', 'now')
            """,
            (task_id, sku, quantity),
        )
        return self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task_id,)).fetchone()

    def insert_ready_task(self, sku="SOP-BAS-58", quantity=1):
        task = self.insert_pending_task(sku, quantity)
        self.mod.update_task(
            self.conn,
            task["task_id"],
            "ready_to_apply",
            "ready",
            shopify_variant_id="41798285164679",
            shopify_inventory_item_id="43895215915143",
            shopify_location_id="84634501255",
            shopify_stock=2,
            shopify_stock_before=2,
            shopify_target_stock=1,
        )
        return self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()

    def test_paid_order_creates_idempotent_task(self):
        self.mod.create_tasks_from_order(self.conn, paid_order())
        self.mod.create_tasks_from_order(self.conn, paid_order())

        tasks = self.rows("stock_tasks")
        logs = self.rows("sync_logs")

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "meli")
        self.assertEqual(tasks[0]["sku"], "SOP-BAS-58")
        self.assertEqual(tasks[0]["quantity_sold"], 1)
        self.assertEqual(tasks[0]["status"], "pending")
        self.assertTrue(any(log["event_type"] == "duplicate_task_ignored" for log in logs))

    def test_non_paid_order_does_not_create_applicable_task(self):
        order = paid_order()
        order["status"] = "cancelled"

        self.mod.create_tasks_from_order(self.conn, order)

        self.assertEqual(len(self.rows("stock_tasks")), 0)
        self.assertEqual(self.rows("sync_logs")[0]["event_type"], "skipped_non_paid")

    def test_missing_sku_creates_review_task(self):
        order = paid_order(sku="")
        order["order_items"][0]["item"].pop("seller_custom_field")
        order["order_items"][0]["item"].pop("id")

        self.mod.create_tasks_from_order(self.conn, order)

        task = self.rows("stock_tasks")[0]
        self.assertEqual(task["status"], "needs_review")
        self.assertIsNone(task["sku"])
        self.assertEqual(task["meli_sku_source"], "missing_item_id")

    def test_fallback_variation_sku_is_used(self):
        order = paid_order(sku="")
        order["order_items"][0]["item"].pop("seller_custom_field")
        order["order_items"][0]["item"]["variation_id"] = "123"

        def fake_fetch_meli_item(item_id):
            return {
                "variations": [
                    {"id": 123, "attributes": [{"id": "SELLER_SKU", "value_name": "SOP-BAS-58"}]},
                ]
            }

        self.mod.fetch_meli_item = fake_fetch_meli_item
        self.mod.create_tasks_from_order(self.conn, order)

        task = self.rows("stock_tasks")[0]
        self.assertEqual(task["sku"], "SOP-BAS-58")
        self.assertEqual(task["meli_sku_source"], "meli_variation")

    def test_dry_run_marks_no_shopify_match_without_stock_change(self):
        task = self.insert_pending_task()

        self.mod.process_meli_task_dry_run(self.conn, task, 84634501255, [])

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(updated["status"], "skipped_not_in_shopify")

    def test_dry_run_marks_duplicate_shopify_sku_as_review(self):
        task = self.insert_pending_task()

        self.mod.process_meli_task_dry_run(
            self.conn,
            task,
            84634501255,
            [shopify_variant(), shopify_variant(variant_id=2, inventory_item_id=3)],
        )

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(updated["status"], "needs_review")
        self.assertIn("duplicado", updated["human_note"])

    def test_dry_run_marks_non_managed_variant_as_review(self):
        task = self.insert_pending_task()
        variant = shopify_variant()
        variant["inventory_management"] = None

        self.mod.process_meli_task_dry_run(self.conn, task, 84634501255, [variant])

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(updated["status"], "needs_review")

    def test_dry_run_computes_target_and_clamps_to_zero(self):
        task = self.insert_pending_task(quantity=3)
        self.mod.get_shopify_stock_for_inventory_item = lambda inventory_item_id, location_id: 2

        self.mod.process_meli_task_dry_run(self.conn, task, 84634501255, [shopify_variant()])

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(updated["status"], "ready_to_apply")
        self.assertEqual(updated["shopify_stock_before"], 2)
        self.assertEqual(updated["shopify_target_stock"], 0)
        self.assertIn("target limitado a 0", updated["human_note"])

    def test_apply_re_reads_sets_confirms_and_marks_synced(self):
        task = self.insert_ready_task(quantity=1)
        stock_reads = [2, 1]
        writes = []
        self.mod.get_shopify_stock_for_inventory_item = lambda inventory_item_id, location_id: stock_reads.pop(0)
        self.mod.set_shopify_inventory_level = lambda location_id, inventory_item_id, quantity: writes.append(quantity) or {}

        self.mod.apply_meli_task(self.conn, task, 84634501255, [shopify_variant()])

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(writes, [1])
        self.assertEqual(updated["status"], "synced")
        self.assertEqual(updated["shopify_stock_before"], 2)
        self.assertEqual(updated["shopify_target_stock"], 1)
        self.assertEqual(updated["shopify_stock"], 1)

    def test_apply_confirmation_mismatch_requires_review(self):
        task = self.insert_ready_task(quantity=1)
        stock_reads = [2, 2]
        self.mod.get_shopify_stock_for_inventory_item = lambda inventory_item_id, location_id: stock_reads.pop(0)
        self.mod.set_shopify_inventory_level = lambda location_id, inventory_item_id, quantity: {}

        self.mod.apply_meli_task(self.conn, task, 84634501255, [shopify_variant()])

        updated = self.conn.execute("SELECT * FROM stock_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
        self.assertEqual(updated["status"], "needs_review")
        self.assertIn("confirmado", updated["human_note"])


if __name__ == "__main__":
    unittest.main()
