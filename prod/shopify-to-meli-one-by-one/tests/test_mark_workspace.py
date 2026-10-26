from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mark_workspace.py"
SPEC = importlib.util.spec_from_file_location("one_by_one_mark_workspace", MODULE_PATH)
assert SPEC and SPEC.loader
marker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = marker
SPEC.loader.exec_module(marker)


class ClassificationTests(unittest.TestCase):
    def test_mapping_is_success_even_if_result_is_an_older_retry(self):
        prefix, reason = marker.classify(
            kind="product_sync",
            shopify_id="product-1",
            state={"mappings": {"product-1": {}}, "blocked": {}, "pending": None},
            result={"outcome": "failed_retryable"},
        )
        self.assertIsNone(prefix)
        self.assertEqual(reason, "published")

    def test_blocked_product_is_failed(self):
        prefix, reason = marker.classify(
            kind="product_sync",
            shopify_id="product-1",
            state={"mappings": {}, "blocked": {"product-1": {}}, "pending": None},
            result={"outcome": "failed_retryable"},
        )
        self.assertEqual(prefix, "[F]")
        self.assertEqual(reason, "blocked")

    def test_retryable_pending_product_is_retry(self):
        prefix, reason = marker.classify(
            kind="product_sync",
            shopify_id="product-1",
            state={
                "mappings": {},
                "blocked": {},
                "pending": {"shopify_id": "product-1"},
            },
            result={"outcome": "failed_retryable"},
        )
        self.assertEqual(prefix, "[R]")
        self.assertEqual(reason, "failed_retryable")

    def test_validation_failure_is_failed_while_product_stays_pending(self):
        prefix, reason = marker.classify(
            kind="product_sync",
            shopify_id="product-1",
            state={
                "mappings": {},
                "blocked": {},
                "pending": {"shopify_id": "product-1"},
            },
            result={"outcome": "validation_failed", "shopify_id": "product-1"},
        )
        self.assertEqual(prefix, "[F]")
        self.assertEqual(reason, "validation_failed")

    def test_preflight_retry_is_retry(self):
        prefix, reason = marker.classify(
            kind="preflight_error",
            shopify_id=None,
            state={},
            result={"outcome": "failed_retryable"},
        )
        self.assertEqual(prefix, "[R]")
        self.assertEqual(reason, "failed_retryable")

    def test_title_cleanup_is_idempotent(self):
        self.assertEqual(marker.clean_title("[F] [R]  Product   name"), "Product name")


class MainTests(unittest.TestCase):
    def test_main_renames_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.json"
            result = root / "result.json"
            state.write_text(
                json.dumps(
                    {
                        "mappings": {},
                        "blocked": {},
                        "pending": {"shopify_id": "product-1"},
                    }
                ),
                encoding="utf-8",
            )
            result.write_text(json.dumps({"outcome": "failed_retryable"}), encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
            with (
                patch.object(marker.subprocess, "run", return_value=completed) as invoked,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = marker.main(
                    [
                        "--workspace-id",
                        "wks-1",
                        "--title",
                        "Product name",
                        "--shopify-id",
                        "product-1",
                        "--state",
                        str(state),
                        "--result",
                        str(result),
                        "--paseo-bin",
                        "paseo",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["title"], "[R] Product name")
            self.assertEqual(
                invoked.call_args.args[0],
                ["paseo", "workspace", "rename", "wks-1", "[R] Product name", "--json"],
            )


if __name__ == "__main__":
    unittest.main()
