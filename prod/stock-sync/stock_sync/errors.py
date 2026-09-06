from __future__ import annotations

from typing import Any


class RetryableSyncError(RuntimeError):
    pass


class ReviewRequiredError(RuntimeError):
    def __init__(self, review_key: str, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.review_key = review_key
        self.details = details
