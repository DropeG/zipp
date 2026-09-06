from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    shopify_shop_url: str
    shopify_access_token: str
    shopify_webhook_secret: str
    meli_app_id: str
    meli_client_secret: str
    meli_expected_seller_id: str
    meli_webhook_token: str
    shopify_api_version: str
    max_webhook_bytes: int
    database_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        required = (
            "SHOPIFY_SHOP_URL",
            "SHOPIFY_ACCESS_TOKEN",
            "SHOPIFY_WEBHOOK_SECRET",
            "MELI_APP_ID",
            "MELI_CLIENT_SECRET",
            "MELI_EXPECTED_SELLER_ID",
            "MELI_WEBHOOK_TOKEN",
        )
        values = {name: os.environ.get(name, "") for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            shopify_shop_url=values["SHOPIFY_SHOP_URL"],
            shopify_access_token=values["SHOPIFY_ACCESS_TOKEN"],
            shopify_webhook_secret=values["SHOPIFY_WEBHOOK_SECRET"],
            meli_app_id=values["MELI_APP_ID"],
            meli_client_secret=values["MELI_CLIENT_SECRET"],
            meli_expected_seller_id=values["MELI_EXPECTED_SELLER_ID"],
            meli_webhook_token=values["MELI_WEBHOOK_TOKEN"],
            shopify_api_version=os.environ.get("SHOPIFY_API_VERSION", "2026-07"),
            max_webhook_bytes=int(os.environ.get("MAX_WEBHOOK_BYTES", "1048576")),
            database_path=os.environ.get("STOCK_SYNC_DATABASE", "prod/stock-sync/data/stock_sync.db"),
        )
