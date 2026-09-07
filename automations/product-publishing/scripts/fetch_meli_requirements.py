#!/usr/bin/env python3
"""Compatibility wrapper for the guarded one-product prepare command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from one_by_one_contract import (  # noqa: E402
    BLOCKED_PRODUCTS_FILE,
    DEFAULT_WORK_DIR,
    MAPPINGS_FILE,
    PUBLICATION_JOURNAL_FILE,
    RESERVATIONS_FILE,
    WORKFLOW_LOCK_FILE,
    workflow_lock,
)
from one_by_one_sync import prepare  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compatibility wrapper: select exactly one Shopify product and "
            "fetch its Mercado Libre requirements."
        )
    )
    parser.add_argument("--mode", choices=("dry-run", "publish"), default="dry-run")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    parser.add_argument("--mappings", default=str(MAPPINGS_FILE))
    parser.add_argument("--blocked-products", default=str(BLOCKED_PRODUCTS_FILE))
    parser.add_argument("--reservations", default=str(RESERVATIONS_FILE))
    parser.add_argument("--journal", default=str(PUBLICATION_JOURNAL_FILE))
    parser.add_argument("--lock", default=str(WORKFLOW_LOCK_FILE))
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    with workflow_lock(Path(arguments.lock).resolve()):
        raise SystemExit(prepare(arguments))
