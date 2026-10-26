from __future__ import annotations

import argparse
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


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dispatch.py"
SPEC = importlib.util.spec_from_file_location("one_by_one_dispatch", MODULE_PATH)
assert SPEC and SPEC.loader
dispatcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatcher
SPEC.loader.exec_module(dispatcher)


def completed(returncode: int = 0, stdout: str = "{}") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.json"
        self.state = self.root / "dispatch.json"
        self.result = self.root / "result.json"
        self.sync_state = self.root / "sync-state.json"
        self.args = argparse.Namespace(
            cwd="/git",
            provider="codex/gpt-test",
            thinking="high",
            mode="full-access",
            paseo_bin="paseo",
            python="python",
            meli_script=str(self.root / "meli.py"),
            outcome_marker=str(self.root / "mark_workspace.py"),
            source=str(self.source),
            result=str(self.result),
            sync_state=str(self.sync_state),
            dispatch_state=str(self.state),
            dispatch_lock=str(self.root / "dispatch.lock"),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_dispatch(self, responses):
        with (
            patch.object(dispatcher, "command_output", side_effect=responses) as invoked,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = dispatcher.dispatch(self.args)
        return code, invoked, json.loads(output.getvalue())

    def test_no_op_does_not_start_paseo(self):
        self.source.write_text(json.dumps({"product": None}), encoding="utf-8")
        code, invoked, output = self.run_dispatch([completed()])
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_count, 1)
        self.assertEqual(output["outcome"], "no-op")
        self.assertFalse(self.state.exists())

    def test_product_starts_one_paseo_chat_and_records_it(self):
        self.source.write_text(
            json.dumps({"product": {"id": "gid://shopify/Product/7", "title": "Hat"}}),
            encoding="utf-8",
        )
        code, invoked, output = self.run_dispatch(
            [
                completed(),
                completed(stdout=json.dumps({"workspaceId": "wks_product"})),
                completed(stdout=json.dumps({"agentId": "agent-7"})),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_count, 3)
        self.assertEqual(output["outcome"], "product_chat_started")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["agent_id"], "agent-7")
        self.assertEqual(state["shopify_id"], "gid://shopify/Product/7")
        self.assertEqual(state["title"], "Hat")
        self.assertEqual(state["workspace_id"], "wks_product")
        workspace_command = invoked.call_args_list[1].args[0]
        self.assertIn("--title", workspace_command)
        self.assertIn("Hat", workspace_command)
        paseo_command = invoked.call_args_list[2].args[0]
        self.assertIn("--workspace", paseo_command)
        self.assertIn("wks_product", paseo_command)
        self.assertIn("PRODUCT_SYNC_PASEO_WORKSPACE_ID=wks_product", paseo_command)
        self.assertIn("PRODUCT_SYNC_PRODUCT_TITLE=Hat", paseo_command)
        self.assertIn("PRODUCT_SYNC_SHOPIFY_ID=gid://shopify/Product/7", paseo_command)

    def test_preflight_failure_starts_diagnostic_chat(self):
        code, invoked, output = self.run_dispatch(
            [
                completed(returncode=4),
                completed(stdout=json.dumps({"workspaceId": "wks_error"})),
                completed(stdout=json.dumps({"id": "agent-error"})),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_count, 3)
        self.assertEqual(output["outcome"], "error_chat_started")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["kind"], "preflight_error")

    def test_running_previous_agent_prevents_duplicate_chat(self):
        self.state.write_text(json.dumps({"agent_id": "agent-running"}), encoding="utf-8")
        status = completed(stdout=json.dumps({"Status": "running", "Archived": False}))
        code, invoked, output = self.run_dispatch([status])
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_count, 1)
        self.assertEqual(output["outcome"], "agent_still_running")

    def test_idle_previous_agent_is_marked_before_next_preflight(self):
        self.state.write_text(
            json.dumps(
                {
                    "agent_id": "agent-idle",
                    "kind": "product_sync",
                    "shopify_id": "product-1",
                    "title": "Product one",
                    "workspace_id": "wks-1",
                }
            ),
            encoding="utf-8",
        )
        self.source.write_text(json.dumps({"product": None}), encoding="utf-8")
        status = completed(stdout=json.dumps({"Status": "idle", "Archived": False}))
        code, invoked, output = self.run_dispatch([status, completed(), completed()])
        self.assertEqual(code, 0)
        self.assertEqual(output["outcome"], "no-op")
        marker_command = invoked.call_args_list[1].args[0]
        self.assertIn(str(self.root / "mark_workspace.py"), marker_command)
        self.assertIn("--workspace-id", marker_command)
        self.assertIn("wks-1", marker_command)
        self.assertIn("--shopify-id", marker_command)
        self.assertIn("product-1", marker_command)


if __name__ == "__main__":
    unittest.main()
