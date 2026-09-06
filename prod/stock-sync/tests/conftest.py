from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 6, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path):
    from stock_sync.db import Database

    return Database(tmp_path / "stock_sync.db")
