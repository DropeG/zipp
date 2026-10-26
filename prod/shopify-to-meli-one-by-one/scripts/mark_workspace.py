#!/usr/bin/env python3
"""Rename a scheduled Paseo workspace from the authoritative sync outcome."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STATE = SKILL_DIR / "data" / "state.json"
DEFAULT_RESULT = SKILL_DIR / "work" / "result.json"
PREFIX_RE = re.compile(r"^(?:\[(?:F|R)\]\s*)+")


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{path.name} is not readable valid JSON") from error


def clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    return PREFIX_RE.sub("", cleaned) or "Shopify to Mercado Libre"


def result_matches_product(result: dict[str, Any], shopify_id: str) -> bool:
    result_id = result.get("shopify_id")
    return result_id is None or str(result_id) == shopify_id


def classify(
    *,
    kind: str,
    shopify_id: str | None,
    state: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str | None, str]:
    outcome = str(result.get("outcome") or "")
    if kind == "preflight_error":
        if outcome == "failed_retryable":
            return "[R]", outcome
        return "[F]", outcome or "preflight_failed"

    if kind != "product_sync" or not shopify_id:
        return "[F]", "invalid_dispatch_metadata"

    mappings = state.get("mappings") if isinstance(state.get("mappings"), dict) else {}
    blocked = state.get("blocked") if isinstance(state.get("blocked"), dict) else {}
    if shopify_id in mappings:
        return None, "published"
    if shopify_id in blocked:
        return "[F]", "blocked"

    matching_outcome = outcome if result_matches_product(result, shopify_id) else ""
    if matching_outcome in {"blocked", "needs_review", "validation_failed"}:
        return "[F]", matching_outcome
    if matching_outcome == "failed_retryable":
        return "[R]", matching_outcome

    pending = state.get("pending")
    if isinstance(pending, dict) and str(pending.get("shopify_id") or "") == shopify_id:
        return "[R]", matching_outcome or "pending"
    return "[F]", matching_outcome or "incomplete"


def rename_workspace(paseo_bin: str, workspace_id: str, title: str) -> None:
    renamed = subprocess.run(
        [paseo_bin, "workspace", "rename", workspace_id, title, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if renamed.returncode != 0:
        raise RuntimeError("Paseo could not rename the product-sync workspace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-id", default=os.environ.get("PRODUCT_SYNC_PASEO_WORKSPACE_ID")
    )
    parser.add_argument("--title", default=os.environ.get("PRODUCT_SYNC_PRODUCT_TITLE"))
    parser.add_argument("--shopify-id", default=os.environ.get("PRODUCT_SYNC_SHOPIFY_ID"))
    parser.add_argument(
        "--kind", default=os.environ.get("PRODUCT_SYNC_DISPATCH_KIND", "product_sync")
    )
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--result", default=str(DEFAULT_RESULT))
    parser.add_argument(
        "--paseo-bin",
        default=os.environ.get("PRODUCT_SYNC_PASEO_BIN", os.environ.get("PASEO_BIN", "paseo")),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.workspace_id or not args.title:
            raise RuntimeError("scheduled Paseo workspace metadata is missing")
        state = read_json(Path(args.state).resolve(), {})
        result = read_json(Path(args.result).resolve(), {})
        if not isinstance(state, dict) or not isinstance(result, dict):
            raise RuntimeError("sync state and result must contain JSON objects")
        prefix, reason = classify(
            kind=args.kind,
            shopify_id=args.shopify_id,
            state=state,
            result=result,
        )
        base_title = clean_title(args.title)
        title = f"{prefix} {base_title}" if prefix else base_title
        rename_workspace(args.paseo_bin, args.workspace_id, title)
        print(
            json.dumps(
                {
                    "outcome": "workspace_renamed",
                    "classification": reason,
                    "title": title,
                    "workspace_id": args.workspace_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"outcome": "workspace_rename_failed", "reason": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
