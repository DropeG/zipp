#!/usr/bin/env python3
"""Thin API and state tool for one-product Shopify -> Mercado Libre runs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from dotenv import load_dotenv


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STATE = SKILL_DIR / "data" / "state.json"
DEFAULT_LOCK = SKILL_DIR / "data" / "sync.lock"
DEFAULT_SOURCE = SKILL_DIR / "work" / "source.json"
DEFAULT_PAYLOAD = SKILL_DIR / "work" / "payload.json"
DEFAULT_RESULT = SKILL_DIR / "work" / "result.json"
DEFAULT_CATEGORY = SKILL_DIR / "work" / "category.json"
DEFAULT_BOOTSTRAP = SKILL_DIR / "work" / "bootstrap.json"
DEFAULT_TOKENS = SKILL_DIR.parent / "stock-sync" / "data" / "meli_tokens.json"
DEFAULT_STOCK_WORKER_LOCK = (
    SKILL_DIR.parent / "stock-sync" / "data" / "stock_sync.db.worker-lock.db"
)

ZIPP_RE = re.compile(r"\b(?:zipp(?:\.cl|\s+chile)?)\b", re.IGNORECASE)
CONTACT_RE = re.compile(
    r"https?://|www\.|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b(?:whats?app|wsp|instagram|facebook|tiktok|contacto)\b|"
    r"(?<!\d)(?:\+?56\s*)?(?:\d[\s().-]*){8,9}(?!\d)",
    re.IGNORECASE,
)
RESERVED_ATTRIBUTES = {"BRAND", "MODEL", "ITEM_CONDITION", "SELLER_SKU", "GTIN", "EMPTY_GTIN_REASON"}


class SyncError(RuntimeError):
    outcome = "failed_retryable"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class ReviewRequired(SyncError):
    outcome = "needs_review"


class ValidationFailed(SyncError):
    outcome = "validation_failed"


class CreateRejected(ReviewRequired):
    """Mercado Libre definitively rejected POST /items without creating."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path(value: str | Path, base: Path = SKILL_DIR) -> Path:
    path = Path(value)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewRequired(f"{path.name} is not readable valid JSON") from error


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SyncError("another one-product command is already running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def stock_worker_lock(path: Path):
    """Serialize Mercado Libre calls with the sibling stock-sync worker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise SyncError("stock-sync is busy; retry this product later") from None
            raise
        yield
    finally:
        connection.close()


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "mappings": {},
        "pending": None,
        "blocked": {},
        "bootstrap": None,
    }


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        state = read_json(self.path, default_state())
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ReviewRequired("state.json is invalid or has an unsupported version")
        if not isinstance(state.get("mappings"), dict) or not isinstance(state.get("blocked"), dict):
            raise ReviewRequired("state mappings and blocked values must be objects")
        if state.get("pending") is not None and not isinstance(state.get("pending"), dict):
            raise ReviewRequired("state pending value must be an object or null")
        if state.get("bootstrap") is not None and not isinstance(state.get("bootstrap"), dict):
            raise ReviewRequired("state bootstrap value must be an object or null")
        return state

    def save(self, state: dict[str, Any]) -> None:
        atomic_json(self.path, state)


@dataclass(frozen=True)
class Settings:
    shopify_url: str
    shopify_token: str
    shopify_version: str
    meli_app_id: str
    meli_client_secret: str
    meli_seller_id: str
    meli_tokens_file: Path
    stock_worker_lock_file: Path
    meli_site_id: str
    listing_type_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "SHOPIFY_SHOP_URL": os.environ.get("SHOPIFY_SHOP_URL", "").strip(),
            "SHOPIFY_ACCESS_TOKEN": os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip(),
            "MELI_APP_ID": os.environ.get("MELI_APP_ID", "").strip(),
            "MELI_CLIENT_SECRET": os.environ.get("MELI_CLIENT_SECRET", "").strip(),
            "MELI_EXPECTED_SELLER_ID": os.environ.get("MELI_EXPECTED_SELLER_ID", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ReviewRequired(f"missing configuration: {', '.join(missing)}")

        raw_shop = required["SHOPIFY_SHOP_URL"].rstrip("/")
        if "://" not in raw_shop:
            raw_shop = f"https://{raw_shop}"
        parsed = urlparse(raw_shop)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ReviewRequired("SHOPIFY_SHOP_URL must be an HTTPS shop origin")

        tokens_value = os.environ.get("PRODUCT_SYNC_MELI_TOKENS_FILE", str(DEFAULT_TOKENS))
        return cls(
            shopify_url=raw_shop,
            shopify_token=required["SHOPIFY_ACCESS_TOKEN"],
            shopify_version=os.environ.get("SHOPIFY_API_VERSION", "2026-07").strip(),
            meli_app_id=required["MELI_APP_ID"],
            meli_client_secret=required["MELI_CLIENT_SECRET"],
            meli_seller_id=required["MELI_EXPECTED_SELLER_ID"],
            meli_tokens_file=_path(tokens_value),
            stock_worker_lock_file=_path(
                os.environ.get(
                    "PRODUCT_SYNC_STOCK_WORKER_LOCK",
                    str(DEFAULT_STOCK_WORKER_LOCK),
                )
            ),
            meli_site_id=os.environ.get("MELI_SITE_ID", "MLC").strip(),
            listing_type_id=os.environ.get("MELI_LISTING_TYPE_ID", "gold_special").strip(),
        )


def load_environment() -> None:
    load_dotenv(SKILL_DIR / ".env", override=False)
    shared_env = os.environ.get("PRODUCT_SYNC_SHARED_ENV_FILE", "").strip()
    if shared_env:
        load_dotenv(_path(shared_env), override=False)
    else:
        load_dotenv(SKILL_DIR.parent / "stock-sync" / ".env", override=False)


class ShopifyClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.endpoint = (
            f"{settings.shopify_url}/admin/api/{settings.shopify_version}/graphql.json"
        )
        self.token = settings.shopify_token
        self.session = session or requests.Session()

    def graphql(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": self.token,
                },
                json={"query": query, "variables": variables},
                timeout=30,
            )
        except requests.RequestException as error:
            raise SyncError(f"Shopify {operation} network failure") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise SyncError(f"Shopify {operation} failed with HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ReviewRequired(f"Shopify {operation} failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise ReviewRequired(f"Shopify {operation} returned malformed JSON") from error
        if payload.get("errors"):
            codes = {
                str(item.get("extensions", {}).get("code"))
                for item in payload["errors"]
                if isinstance(item, dict)
            }
            message = "; ".join(
                str(item.get("message", "Shopify GraphQL error"))
                for item in payload["errors"]
                if isinstance(item, dict)
            )
            if {"THROTTLED", "INTERNAL_SERVER_ERROR"} & codes:
                raise SyncError(f"Shopify {operation}: {message}")
            raise ReviewRequired(f"Shopify {operation}: {message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ReviewRequired(f"Shopify {operation} returned no data")
        return data

    @staticmethod
    def _product_fields() -> str:
        return """
          id legacyResourceId title descriptionHtml vendor productType status tags
          options { name values }
          images(first: 100) {
            nodes { url altText }
            pageInfo { hasNextPage endCursor }
          }
          variants(first: 100) {
            nodes {
              id legacyResourceId title sku barcode price inventoryQuantity
              inventoryItem { tracked }
              selectedOptions { name value }
              image { url altText }
            }
            pageInfo { hasNextPage endCursor }
          }
        """

    def _product_refs(self, query_filter: str | None) -> list[dict[str, str]]:
        products: list[dict[str, str]] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            data = self.graphql(
                "product_refs",
                """
                query Products($after: String, $query: String) {
                  shop { currencyCode }
                  products(first: 100, after: $after, query: $query, sortKey: ID) {
                    nodes { id title status }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                {"after": after, "query": query_filter},
            )
            if str((data.get("shop") or {}).get("currencyCode") or "") != "CLP":
                raise ReviewRequired("Shopify store currency must be CLP")
            connection = data.get("products") or {}
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo") or {}
            if not isinstance(nodes, list):
                raise ReviewRequired("Shopify returned an invalid product page")
            for node in nodes:
                if not isinstance(node, dict) or not node.get("id"):
                    raise ReviewRequired("Shopify returned a product without an ID")
                products.append(
                    {
                        "id": str(node["id"]),
                        "title": str(node.get("title") or ""),
                        "status": str(node.get("status") or ""),
                    }
                )
            if not page_info.get("hasNextPage"):
                return products
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after or after in seen or not nodes:
                raise ReviewRequired("Shopify product pagination is incomplete")
            seen.add(after)

    def active_product_refs(self) -> list[dict[str, str]]:
        return self._product_refs("status:active")

    def all_product_refs(self) -> list[dict[str, str]]:
        return self._product_refs(None)

    def all_variant_refs(self) -> list[dict[str, str]]:
        variants: list[dict[str, str]] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            data = self.graphql(
                "all_variant_refs",
                """
                query ProductVariants($after: String) {
                  productVariants(first: 100, after: $after, sortKey: ID) {
                    nodes { id sku product { id status } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                {"after": after},
            )
            connection = data.get("productVariants") or {}
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo") or {}
            if not isinstance(nodes, list):
                raise ReviewRequired("Shopify returned an invalid variant identity page")
            for node in nodes:
                product = node.get("product") if isinstance(node, dict) else None
                if (
                    not isinstance(node, dict)
                    or not node.get("id")
                    or not isinstance(product, dict)
                    or not product.get("id")
                ):
                    raise ReviewRequired("Shopify returned a variant without product identity")
                variants.append(
                    {
                        "id": str(node["id"]),
                        "sku": str(node.get("sku") or "").strip(),
                        "product_id": str(product["id"]),
                        "product_status": str(product.get("status") or ""),
                    }
                )
            if not page_info.get("hasNextPage"):
                return variants
            after = page_info.get("endCursor")
            if not isinstance(after, str) or not after or after in seen or not nodes:
                raise ReviewRequired("Shopify variant identity pagination is incomplete")
            seen.add(after)

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        data = self.graphql(
            "get_product",
            f"""
            query Product($id: ID!) {{
              product(id: $id) {{ {self._product_fields()} }}
            }}
            """,
            {"id": product_id},
        )
        product = data.get("product")
        return self._complete_product(product) if isinstance(product, dict) else None

    def _complete_product(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = dict(raw)
        product_id = str(raw.get("id") or "")
        if not product_id:
            raise ReviewRequired("Shopify returned a product without an ID")

        variants = list((raw.get("variants") or {}).get("nodes") or [])
        variant_page = (raw.get("variants") or {}).get("pageInfo") or {}
        after = variant_page.get("endCursor")
        seen: set[str] = set()
        while variant_page.get("hasNextPage"):
            if not isinstance(after, str) or not after or after in seen:
                raise ReviewRequired(f"Shopify variant pagination is incomplete for {product_id}")
            seen.add(after)
            data = self.graphql(
                "product_variants",
                """
                query ProductVariants($id: ID!, $after: String!) {
                  product(id: $id) {
                    variants(first: 100, after: $after) {
                      nodes {
                        id legacyResourceId title sku barcode price inventoryQuantity
                        inventoryItem { tracked }
                        selectedOptions { name value }
                        image { url altText }
                      }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                {"id": product_id, "after": after},
            )
            connection = ((data.get("product") or {}).get("variants") or {})
            page_nodes = connection.get("nodes") or []
            if not isinstance(page_nodes, list) or not page_nodes:
                raise ReviewRequired(f"Shopify variant pagination is incomplete for {product_id}")
            variants.extend(page_nodes)
            variant_page = connection.get("pageInfo") or {}
            after = variant_page.get("endCursor")

        images = list((raw.get("images") or {}).get("nodes") or [])
        image_page = (raw.get("images") or {}).get("pageInfo") or {}
        after = image_page.get("endCursor")
        seen.clear()
        while image_page.get("hasNextPage"):
            if not isinstance(after, str) or not after or after in seen:
                raise ReviewRequired(f"Shopify image pagination is incomplete for {product_id}")
            seen.add(after)
            data = self.graphql(
                "product_images",
                """
                query ProductImages($id: ID!, $after: String!) {
                  product(id: $id) {
                    images(first: 100, after: $after) {
                      nodes { url altText }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
                """,
                {"id": product_id, "after": after},
            )
            connection = ((data.get("product") or {}).get("images") or {})
            page_nodes = connection.get("nodes") or []
            if not isinstance(page_nodes, list) or not page_nodes:
                raise ReviewRequired(f"Shopify image pagination is incomplete for {product_id}")
            images.extend(page_nodes)
            image_page = connection.get("pageInfo") or {}
            after = image_page.get("endCursor")

        return normalize_product({**raw, "variants": variants, "images": images})


class MeliClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.token_lock = settings.meli_tokens_file.with_suffix(
            settings.meli_tokens_file.suffix + ".lock"
        )

    def _load_tokens(self) -> dict[str, Any]:
        try:
            tokens = read_json(self.settings.meli_tokens_file, None)
            if not isinstance(tokens, dict):
                raise ValueError("not an object")
            if not str(tokens.get("access_token") or "") or not str(tokens.get("refresh_token") or ""):
                raise ValueError("missing token")
            expires_at = tokens.get("expires_at")
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at)
            ):
                raise ValueError("invalid expiry")
            return tokens
        except (OSError, ValueError, TypeError) as error:
            raise ReviewRequired("Mercado Libre token file is missing or invalid") from error

    def _refresh(self, tokens: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                "https://api.mercadolibre.com/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.meli_app_id,
                    "client_secret": self.settings.meli_client_secret,
                    "refresh_token": tokens["refresh_token"],
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise SyncError("Mercado Libre token refresh network failure") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise SyncError(f"Mercado Libre token refresh failed with HTTP {response.status_code}")
        if not response.ok:
            raise ReviewRequired(f"Mercado Libre token refresh failed with HTTP {response.status_code}")
        try:
            refreshed = response.json()
            access_token = str(refreshed["access_token"])
            refresh_token = str(refreshed["refresh_token"])
            expires_in = int(refreshed["expires_in"])
        except (ValueError, KeyError, TypeError) as error:
            raise ReviewRequired("Mercado Libre token refresh returned invalid data") from error
        if not access_token or not refresh_token or expires_in <= 0:
            raise ReviewRequired("Mercado Libre token refresh returned incomplete data")
        if refreshed.get("user_id") is not None and str(refreshed["user_id"]) != self.settings.meli_seller_id:
            raise ReviewRequired("Mercado Libre refreshed a token for another seller")
        refreshed["expires_at"] = time.time() + expires_in
        atomic_json(self.settings.meli_tokens_file, refreshed)
        return refreshed

    def _access_token(self, force_refresh: bool = False) -> str:
        with exclusive_lock(self.token_lock):
            tokens = self._load_tokens()
            if force_refresh or time.time() >= float(tokens["expires_at"]) - 120:
                previous_access = str(tokens["access_token"])
                try:
                    tokens = self._refresh(tokens)
                except ReviewRequired:
                    latest = self._load_tokens()
                    if str(latest["access_token"]) == previous_access:
                        raise
                    tokens = latest
            return str(tokens["access_token"])

    def raw(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        token = self._access_token()
        try:
            response = self.session.request(
                method,
                f"https://api.mercadolibre.com{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as error:
            raise SyncError(f"Mercado Libre {method} {path} network failure") from error
        if response.status_code == 401:
            token = self._access_token(force_refresh=True)
            try:
                response = self.session.request(
                    method,
                    f"https://api.mercadolibre.com{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=30,
                    **kwargs,
                )
            except requests.RequestException as error:
                raise SyncError(f"Mercado Libre {method} {path} network failure") from error
        return response

    @staticmethod
    def _payload(response: requests.Response, path: str) -> Any:
        if response.status_code == 429 or response.status_code >= 500:
            raise SyncError(f"Mercado Libre {path} failed with HTTP {response.status_code}")
        if not response.ok:
            try:
                details = response.json()
            except ValueError:
                details = {"status": response.status_code}
            raise ReviewRequired(
                f"Mercado Libre {path} failed with HTTP {response.status_code}", details
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise ReviewRequired(f"Mercado Libre {path} returned malformed JSON") from error

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._payload(self.raw(method, path, **kwargs), path)

    def verify_seller(self) -> dict[str, Any]:
        seller = self.request("GET", "/users/me")
        if not isinstance(seller, dict) or str(seller.get("id") or "") != self.settings.meli_seller_id:
            raise ReviewRequired("Mercado Libre token does not belong to the configured seller")
        return seller

    def discover_categories(self, query_text: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/sites/{quote(self.settings.meli_site_id, safe='')}/domain_discovery/search",
            params={"q": query_text},
        )
        if not isinstance(payload, list):
            raise ReviewRequired("Mercado Libre category discovery returned invalid data")
        result = []
        for row in payload[:10]:
            if not isinstance(row, dict) or not row.get("category_id"):
                continue
            result.append(
                {
                    "category_id": str(row["category_id"]),
                    "category_name": row.get("category_name"),
                    "domain_id": row.get("domain_id"),
                    "domain_name": row.get("domain_name"),
                }
            )
        return result

    def category_attributes(self, category_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/categories/{quote(category_id, safe='')}/attributes")
        if not isinstance(payload, list):
            raise ReviewRequired("Mercado Libre category attributes returned invalid data")
        return payload

    def validate_item(self, item: dict[str, Any]) -> dict[str, Any]:
        response = self.raw("POST", "/items/validate", json=item)
        if response.status_code in {200, 204}:
            return {"valid": True, "status": response.status_code, "warnings": []}
        if response.status_code == 429 or response.status_code >= 500:
            raise SyncError(f"Mercado Libre validation failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": "non-JSON validation response"}
        causes = payload.get("cause") if isinstance(payload, dict) else []
        causes = causes if isinstance(causes, list) else []
        errors = [cause for cause in causes if isinstance(cause, dict) and cause.get("type") == "error"]
        warnings = [cause for cause in causes if isinstance(cause, dict) and cause.get("type") == "warning"]
        valid = response.status_code == 400 and bool(warnings) and not errors
        return {
            "valid": valid,
            "status": response.status_code,
            "errors": errors,
            "warnings": warnings,
            "response": payload,
        }

    def create_item(self, item: dict[str, Any]) -> dict[str, Any]:
        response = self.raw("POST", "/items", json=item)
        if 400 <= response.status_code < 500 and response.status_code not in {
            408,
            409,
            425,
            429,
        }:
            try:
                details = response.json()
            except ValueError:
                details = {"status": response.status_code}
            raise CreateRejected(
                f"Mercado Libre rejected item creation with HTTP {response.status_code}",
                details,
            )
        payload = self._payload(response, "/items")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise SyncError("Mercado Libre create returned no item ID; outcome is uncertain")
        return payload

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        response = self.raw("GET", f"/items/{quote(item_id, safe='')}")
        if response.status_code == 404:
            return None
        payload = self._payload(response, f"/items/{item_id}")
        if not isinstance(payload, dict):
            raise ReviewRequired(f"Mercado Libre item {item_id} returned invalid data")
        return payload

    def get_description(self, item_id: str) -> str | None:
        response = self.raw("GET", f"/items/{quote(item_id, safe='')}/description")
        if response.status_code == 404:
            return None
        payload = self._payload(response, f"/items/{item_id}/description")
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("plain_text") or "")

    def set_description(self, item_id: str, description: str) -> None:
        existing = self.get_description(item_id)
        method = "POST" if existing is None else "PUT"
        self.request(
            method,
            f"/items/{quote(item_id, safe='')}/description",
            json={"plain_text": description},
        )

    def set_status(self, item_id: str, status: str) -> None:
        self.request("PUT", f"/items/{quote(item_id, safe='')}", json={"status": status})

    def search_exact_sku(self, sku: str) -> list[str]:
        item_ids: dict[str, None] = {}
        for field in ("sku", "seller_sku"):
            offset = 0
            while True:
                payload = self.request(
                    "GET",
                    f"/users/{quote(self.settings.meli_seller_id, safe='')}/items/search",
                    params={field: sku, "limit": 50, "offset": offset},
                )
                if not isinstance(payload, dict):
                    raise ReviewRequired("Mercado Libre SKU search returned invalid data")
                results = payload.get("results") or []
                if not isinstance(results, list):
                    raise ReviewRequired("Mercado Libre SKU search results are invalid")
                for item_id in results:
                    item_ids[str(item_id)] = None
                total = int((payload.get("paging") or {}).get("total") or len(results))
                offset += len(results)
                if offset >= total:
                    break
                if not results:
                    raise ReviewRequired("Mercado Libre SKU search pagination is incomplete")
        exact = []
        for item_id in item_ids:
            item = self.get_item(item_id)
            if item and sku in item_sku_values(item):
                exact.append(item_id)
        return exact

    def all_seller_item_ids(self) -> list[str]:
        """Scan every item in the configured seller account, regardless of status."""
        item_ids: dict[str, None] = {}
        scroll_id: str | None = None
        for _page in range(10000):
            params: dict[str, Any] = {"search_type": "scan", "limit": 100}
            if scroll_id:
                params["scroll_id"] = scroll_id
            payload = self.request(
                "GET",
                f"/users/{quote(self.settings.meli_seller_id, safe='')}/items/search",
                params=params,
            )
            if payload is None:
                return list(item_ids)
            if not isinstance(payload, dict):
                raise ReviewRequired("Mercado Libre seller scan returned invalid data")
            results = payload.get("results")
            if results is None:
                return list(item_ids)
            if not isinstance(results, list):
                raise ReviewRequired("Mercado Libre seller scan results are invalid")
            if not results:
                return list(item_ids)
            before = len(item_ids)
            for item_id in results:
                value = str(item_id or "").strip()
                if not value:
                    raise ReviewRequired("Mercado Libre seller scan returned an empty item ID")
                item_ids[value] = None
            if len(item_ids) == before:
                raise ReviewRequired("Mercado Libre seller scan repeated a page")
            next_scroll = payload.get("scroll_id")
            if not isinstance(next_scroll, str) or not next_scroll:
                total = int((payload.get("paging") or {}).get("total") or len(item_ids))
                if len(item_ids) >= total:
                    return list(item_ids)
                raise ReviewRequired("Mercado Libre seller scan pagination is incomplete")
            scroll_id = next_scroll
        raise ReviewRequired("Mercado Libre seller scan exceeded the pagination safety limit")


def normalize_product(raw: dict[str, Any]) -> dict[str, Any]:
    variants = []
    for node in raw.get("variants") or []:
        quantity = node.get("inventoryQuantity")
        variants.append(
            {
                "id": str(node.get("id") or ""),
                "legacy_id": str(node.get("legacyResourceId") or ""),
                "title": str(node.get("title") or ""),
                "sku": str(node.get("sku") or "").strip(),
                "barcode": str(node.get("barcode") or "").strip(),
                "price": str(node.get("price") or ""),
                "inventory_quantity": quantity if type(quantity) is int else None,
                "inventory_tracked": (
                    (node.get("inventoryItem") or {}).get("tracked")
                    if isinstance(node.get("inventoryItem"), dict)
                    else None
                ),
                "selected_options": [
                    {"name": str(option.get("name") or ""), "value": str(option.get("value") or "")}
                    for option in node.get("selectedOptions") or []
                    if isinstance(option, dict)
                ],
                "image": (
                    {
                        "url": str((node.get("image") or {}).get("url") or ""),
                        "alt_text": (node.get("image") or {}).get("altText"),
                    }
                    if isinstance(node.get("image"), dict)
                    else None
                ),
            }
        )
    images = [
        {"url": str(image.get("url") or ""), "alt_text": image.get("altText")}
        for image in raw.get("images") or []
        if isinstance(image, dict) and image.get("url")
    ]
    return {
        "id": str(raw.get("id") or ""),
        "legacy_id": str(raw.get("legacyResourceId") or ""),
        "title": str(raw.get("title") or ""),
        "description_html": str(raw.get("descriptionHtml") or ""),
        "vendor": str(raw.get("vendor") or ""),
        "product_type": str(raw.get("productType") or ""),
        "status": str(raw.get("status") or ""),
        "tags": [str(tag) for tag in raw.get("tags") or []],
        "options": [
            {"name": str(option.get("name") or ""), "values": [str(value) for value in option.get("values") or []]}
            for option in raw.get("options") or []
            if isinstance(option, dict)
        ],
        "images": images,
        "variants": variants,
    }


def decimal_price(value: Any) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReviewRequired(f"invalid Shopify price {value!r}") from error
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ReviewRequired(f"Shopify CLP price must be a positive whole number: {value!r}")
    return price


def sellable(variant: dict[str, Any]) -> bool:
    try:
        return (
            type(variant.get("inventory_quantity")) is int
            and variant["inventory_quantity"] > 0
            and decimal_price(variant.get("price")) > 0
        )
    except ReviewRequired:
        return False


def product_issues(product: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    variants = product.get("variants") or []
    if not variants:
        issues.append("Shopify product has no variants")
    if not product.get("images"):
        issues.append("Shopify product has no images")
    skus = [str(variant.get("sku") or "").strip() for variant in variants]
    if any(not sku for sku in skus):
        issues.append("every Shopify variant requires a non-empty SKU")
    duplicates = sorted({sku for sku in skus if sku and skus.count(sku) > 1})
    if duplicates:
        issues.append(f"Shopify SKUs must be unique within the product: {duplicates}")
    for variant in variants:
        if variant.get("inventory_tracked") is not True:
            issues.append(f"variant {variant.get('id')} does not have tracked Shopify inventory")
        if type(variant.get("inventory_quantity")) is not int:
            issues.append(f"variant {variant.get('id')} has unknown inventory")
        elif variant["inventory_quantity"] < 0:
            issues.append(f"variant {variant.get('id')} has negative inventory")
        try:
            price = Decimal(str(variant.get("price")))
            if not price.is_finite() or price < 0 or price != price.to_integral_value():
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            issues.append(
                f"variant {variant.get('id')} has an invalid whole-number CLP price"
            )
    return list(dict.fromkeys(issues))


def target_variant_ids(product: dict[str, Any], mapping: Any, publication_model: str) -> list[str]:
    if publication_model == "legacy_variations":
        return [str(variant["id"]) for variant in product.get("variants") or []]
    mapped = set((mapping or {}).get("variants") or {}) if isinstance(mapping, dict) else set()
    return [
        str(variant["id"])
        for variant in product.get("variants") or []
        if sellable(variant) and str(variant["id"]) not in mapped
    ]


def source_fingerprint(product: dict[str, Any]) -> str:
    relevant = {
        "id": product.get("id"),
        "title": product.get("title"),
        "description_html": product.get("description_html"),
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "images": product.get("images"),
        "variants": product.get("variants"),
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def clean_attribute(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not str(value.get("id") or "").strip():
        raise ReviewRequired(f"{context} contains an attribute without an id")
    result = {"id": str(value["id"]).strip()}
    if value.get("value_id") is not None and str(value.get("value_id")).strip():
        result["value_id"] = str(value["value_id"]).strip()
    if value.get("value_name") is not None and str(value.get("value_name")).strip():
        result["value_name"] = str(value["value_name"]).strip()
    if len(result) == 1:
        raise ReviewRequired(f"{context} attribute {result['id']} has no value")
    return result


def _variant_by_id(product: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(variant.get("id") or ""): variant for variant in product.get("variants") or []}


def _identifier_attributes(variant: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = [{"id": "SELLER_SKU", "value_name": str(variant["sku"])}]
    barcode = str(variant.get("barcode") or "").strip()
    if barcode and (not barcode.isdigit() or len(barcode) not in {8, 12, 13, 14}):
        raise ReviewRequired(f"variant {variant.get('id')} has an invalid GTIN barcode")
    if barcode:
        attributes.append({"id": "GTIN", "value_name": barcode})
    else:
        attributes.append({"id": "EMPTY_GTIN_REASON", "value_id": "17055160"})
    return attributes


def _deduplicate_attributes(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attribute in attributes:
        attribute_id = str(attribute.get("id") or "")
        if attribute_id and attribute_id not in seen:
            result.append(attribute)
            seen.add(attribute_id)
    return result


def build_candidates(
    source: dict[str, Any], payload: dict[str, Any], listing_type_id: str
) -> list[dict[str, Any]]:
    product = source.get("product")
    if not isinstance(product, dict):
        raise ReviewRequired("source.json contains no Shopify product")
    if str(payload.get("shopify_id") or "") != str(product.get("id") or ""):
        raise ReviewRequired("payload shopify_id does not match source.json")
    issues = product_issues(product)
    if issues:
        raise ReviewRequired("; ".join(issues))

    category_id = str(payload.get("category_id") or "").strip()
    if not category_id.startswith("MLC"):
        raise ReviewRequired("payload category_id must be a Mercado Libre Chile category")
    description = str(payload.get("description") or "").strip()
    brand = str(payload.get("brand") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not description or not brand or not model:
        raise ReviewRequired("payload description, brand, and model are required")

    publication_model = str(source.get("publication_model") or "")
    identity_value = (
        payload.get("family_name")
        if publication_model == "user_products_family"
        else payload.get("title")
    )
    identity = str(identity_value or "").strip()
    if not identity:
        raise ReviewRequired(
            "payload family_name is required for User Products"
            if publication_model == "user_products_family"
            else "payload title is required for legacy publication"
        )
    for field_name, value in {
        "title or family_name": identity,
        "description": description,
        "brand": brand,
        "model": model,
    }.items():
        if ZIPP_RE.search(value) or CONTACT_RE.search(value):
            raise ReviewRequired(f"payload {field_name} contains store or contact information")

    source_images = {str(image.get("url")) for image in product.get("images") or []}
    pictures = [str(value) for value in payload.get("pictures") or []]
    if not pictures or len(pictures) != len(set(pictures)) or any(url not in source_images for url in pictures):
        raise ReviewRequired("payload pictures must be unique Shopify source image URLs")

    extra_attributes = [
        clean_attribute(attribute, "payload attributes")
        for attribute in payload.get("attributes") or []
    ]
    extra_ids = [attribute["id"] for attribute in extra_attributes]
    if len(extra_ids) != len(set(extra_ids)):
        raise ReviewRequired("payload attributes contain duplicate IDs")
    forbidden = sorted(RESERVED_ATTRIBUTES.intersection(extra_ids))
    if forbidden:
        raise ReviewRequired(f"payload must not override script-owned attributes: {forbidden}")

    variant_attributes_raw = payload.get("variant_attributes") or {}
    variant_pictures_raw = payload.get("variant_pictures") or {}
    if not isinstance(variant_attributes_raw, dict) or not isinstance(variant_pictures_raw, dict):
        raise ReviewRequired("variant_attributes and variant_pictures must be objects")

    variants = _variant_by_id(product)
    targets = [str(value) for value in source.get("target_variant_ids") or []]
    if not targets or any(target not in variants for target in targets):
        raise ReviewRequired("source target variants are invalid")

    combinations: dict[str, list[dict[str, Any]]] = {}
    combination_keys: set[str] = set()
    multi_variant = len(product.get("variants") or []) > 1
    for variant_id in targets:
        raw_list = variant_attributes_raw.get(variant_id) or []
        if not isinstance(raw_list, list):
            raise ReviewRequired(f"variant_attributes[{variant_id}] must be a list")
        cleaned = [clean_attribute(attribute, f"variant {variant_id}") for attribute in raw_list]
        ids = [attribute["id"] for attribute in cleaned]
        if len(ids) != len(set(ids)) or RESERVED_ATTRIBUTES.intersection(ids):
            raise ReviewRequired(f"variant {variant_id} has duplicate or script-owned attributes")
        if multi_variant and not cleaned:
            raise ReviewRequired(f"variant {variant_id} requires source-faithful option attributes")
        key = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
        if multi_variant and key in combination_keys:
            raise ReviewRequired("variant attribute combinations must be unique")
        combination_keys.add(key)
        combinations[variant_id] = cleaned

    common_attributes = _deduplicate_attributes(
        [
            {"id": "ITEM_CONDITION", "value_id": "2230284"},
            {"id": "BRAND", "value_name": brand},
            {"id": "MODEL", "value_name": model},
            *extra_attributes,
        ]
    )
    shipping_mode = str(payload.get("shipping_mode") or "me2").strip()
    if shipping_mode not in {"me2", "not_specified"}:
        raise ReviewRequired("payload shipping_mode must be me2 or not_specified")
    common = {
        "category_id": category_id,
        "currency_id": "CLP",
        "buying_mode": "buy_it_now",
        "listing_type_id": listing_type_id,
        "condition": "new",
        "channels": ["marketplace"],
        "shipping": {
            "mode": shipping_mode,
            "local_pick_up": True,
            "free_shipping": True,
        },
    }

    if publication_model == "legacy_variations":
        selected = [variants[variant_id] for variant_id in targets]
        sellable_variants = [variant for variant in selected if sellable(variant)]
        if not sellable_variants:
            raise ReviewRequired("legacy publication has no sellable variant")
        variation_ids = {
            attribute["id"] for values in combinations.values() for attribute in values
        }
        top_attributes = [
            attribute for attribute in common_attributes if attribute["id"] not in variation_ids
        ]
        body: dict[str, Any] = {
            **common,
            "title": identity,
            "price": int(decimal_price(sellable_variants[0]["price"])),
            "pictures": [{"source": url} for url in pictures],
            "attributes": top_attributes,
        }
        if len(selected) == 1:
            variant = selected[0]
            body["available_quantity"] = int(variant["inventory_quantity"])
            body["seller_custom_field"] = variant["sku"]
            body["attributes"] = _deduplicate_attributes(
                [*top_attributes, *_identifier_attributes(variant)]
            )
        else:
            body["variations"] = [
                {
                    "attribute_combinations": combinations[str(variant["id"])],
                    "available_quantity": int(variant["inventory_quantity"]),
                    "price": int(decimal_price(variant["price"])),
                    "seller_custom_field": variant["sku"],
                    "attributes": _identifier_attributes(variant),
                }
                for variant in selected
            ]
        return [
            {
                "key": "legacy",
                "variant_ids": targets,
                "skus": [str(variant["sku"]) for variant in selected],
                "body": body,
                "description": description,
                "publication_model": publication_model,
            }
        ]

    if publication_model != "user_products_family":
        raise ReviewRequired(f"unsupported publication model: {publication_model}")
    existing_mapping = source.get("existing_mapping")
    if isinstance(existing_mapping, dict) and existing_mapping.get("family_name"):
        expected_family = str(existing_mapping["family_name"])
        if normalize_text(identity) != normalize_text(expected_family):
            raise ReviewRequired("family expansion must preserve the mapped family_name")
        identity = expected_family

    candidates = []
    for variant_id in targets:
        variant = variants[variant_id]
        if not sellable(variant):
            raise ReviewRequired(f"User Product variant {variant_id} is not currently sellable")
        variant_pictures = [str(value) for value in variant_pictures_raw.get(variant_id) or pictures]
        if (
            not variant_pictures
            or len(variant_pictures) != len(set(variant_pictures))
            or any(url not in source_images for url in variant_pictures)
        ):
            raise ReviewRequired(
                f"variant {variant_id} pictures must be unique Shopify source URLs"
            )
        body = {
            **common,
            "family_name": identity,
            "price": int(decimal_price(variant["price"])),
            "available_quantity": int(variant["inventory_quantity"]),
            "seller_custom_field": variant["sku"],
            "pictures": [{"source": url} for url in variant_pictures],
            "attributes": _deduplicate_attributes(
                [*common_attributes, *combinations[variant_id], *_identifier_attributes(variant)]
            ),
        }
        candidates.append(
            {
                "key": variant_id,
                "variant_ids": [variant_id],
                "skus": [str(variant["sku"])],
                "body": body,
                "description": description,
                "publication_model": publication_model,
            }
        )
    return candidates


def attribute_value(attributes: Any, attribute_id: str) -> str:
    for attribute in attributes or []:
        if isinstance(attribute, dict) and attribute.get("id") == attribute_id:
            return str(attribute.get("value_id") or attribute.get("value_name") or "").strip()
    return ""


def item_sku_values(item: dict[str, Any]) -> set[str]:
    """Return every non-empty SKU representation, including conflicting ones."""
    variations = item.get("variations") or []
    raw_rows = [item]
    if isinstance(variations, list):
        raw_rows.extend(variations)
    values: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        custom_sku = str(raw.get("seller_custom_field") or "").strip()
        seller_sku = attribute_value(raw.get("attributes"), "SELLER_SKU")
        if custom_sku:
            values.add(custom_sku)
        if seller_sku:
            values.add(seller_sku)
    return values


def item_sku_rows(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    variations = item.get("variations") or []
    if variations:
        for variation in variations:
            if not isinstance(variation, dict):
                continue
            custom_sku = str(variation.get("seller_custom_field") or "").strip()
            seller_sku = attribute_value(variation.get("attributes"), "SELLER_SKU")
            sku = custom_sku or seller_sku
            if sku:
                duplicate = sku in rows
                rows[sku] = {
                    "price": variation.get("price", item.get("price")),
                    "quantity": variation.get("available_quantity"),
                    "variation_id": variation.get("id"),
                    "custom_sku": custom_sku,
                    "seller_sku": seller_sku,
                    "duplicate": duplicate,
                }
        return rows
    custom_sku = str(item.get("seller_custom_field") or "").strip()
    seller_sku = attribute_value(item.get("attributes"), "SELLER_SKU")
    sku = custom_sku or seller_sku
    if sku:
        rows[sku] = {
            "price": item.get("price"),
            "quantity": item.get("available_quantity"),
            "variation_id": None,
            "custom_sku": custom_sku,
            "seller_sku": seller_sku,
            "duplicate": False,
        }
    return rows


def bootstrap_item_rows(
    item: dict[str, Any], seller_id: str, site_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract trustworthy SKU identities without requiring new-publisher fields."""
    item_id = str(item.get("id") or "").strip()
    issues: list[dict[str, Any]] = []
    if not item_id:
        return [], [{"type": "meli_item_missing_id"}]
    if str(item.get("seller_id") or "") != seller_id:
        return [], [
            {
                "type": "meli_seller_mismatch",
                "item_id": item_id,
                "seller_id": str(item.get("seller_id") or ""),
            }
        ]
    live_site = str(item.get("site_id") or "").strip()
    if live_site and live_site != site_id:
        return [], [
            {
                "type": "meli_site_mismatch",
                "item_id": item_id,
                "site_id": live_site,
            }
        ]
    status = str(item.get("status") or "").strip()
    raw_variations = item.get("variations") or []
    if not isinstance(raw_variations, list):
        return [], [
            {
                "type": "meli_variations_invalid",
                "item_id": item_id,
                "status": status,
                "skus": sorted(item_sku_values(item)),
            }
        ]
    has_variations = bool(raw_variations)
    raw_rows: list[tuple[Any, dict[str, Any]]] = (
        [(index, value) for index, value in enumerate(raw_variations)]
        if has_variations
        else [(None, item)]
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in raw_rows:
        if not isinstance(raw, dict):
            issues.append(
                {
                    "type": "meli_sku_row_invalid",
                    "item_id": item_id,
                    "variation_index": index,
                    "status": status,
                }
            )
            continue
        custom_sku = str(raw.get("seller_custom_field") or "").strip()
        seller_sku = attribute_value(raw.get("attributes"), "SELLER_SKU")
        if not custom_sku and not seller_sku:
            issues.append(
                {
                    "type": "meli_sku_missing",
                    "item_id": item_id,
                    "variation_index": index,
                    "status": status,
                }
            )
            continue
        if custom_sku and seller_sku and custom_sku != seller_sku:
            issues.append(
                {
                    "type": "meli_sku_fields_conflict",
                    "item_id": item_id,
                    "variation_index": index,
                    "seller_custom_field": custom_sku,
                    "seller_sku": seller_sku,
                }
            )
            continue
        sku = custom_sku or seller_sku
        if sku in seen:
            issues.append(
                {
                    "type": "meli_sku_repeated_within_item",
                    "item_id": item_id,
                    "sku": sku,
                }
            )
            continue
        seen.add(sku)
        variation_id = raw.get("id") if has_variations else None
        rows.append(
            {
                "sku": sku,
                "item_id": item_id,
                "variation_id": str(variation_id) if variation_id is not None else None,
                "user_product_id": str(item.get("user_product_id") or "").strip(),
                "family_id": str(item.get("family_id") or "").strip(),
                "family_name": str(item.get("family_name") or "").strip(),
                "has_variations": has_variations,
                "status": status,
            }
        )

    if status not in {"active", "paused"}:
        issues.append(
            {
                "type": "meli_item_not_currently_publishable",
                "item_id": item_id,
                "status": status,
                "skus": sorted(item_sku_values(item)),
            }
        )

    # A malformed row makes the entire item unsafe to infer, especially for a
    # legacy listing whose variations must be treated as one publication.
    return ([], issues) if issues else (rows, [])


def _bootstrap_variant_mapping(link: dict[str, Any]) -> dict[str, Any]:
    row = link["meli"]
    return {
        "sku": link["sku"],
        "meli_item_id": row["item_id"],
        "meli_variation_id": row["variation_id"],
        "user_product_id": row["user_product_id"] or None,
    }


def current_sku_holds(
    variant_refs: list[dict[str, Any]], mappings: dict[str, Any]
) -> dict[str, list[str]]:
    """Detect Shopify SKU reuse and drift against already published mappings."""
    holds: dict[str, set[str]] = {}

    def hold(product_id: str, reason: str) -> None:
        if product_id:
            holds.setdefault(product_id, set()).add(reason)

    current_by_variant: dict[str, dict[str, str]] = {}
    current_by_sku: dict[str, list[dict[str, str]]] = {}
    for raw in variant_refs:
        variant_id = str(raw.get("id") or "").strip()
        product_id = str(raw.get("product_id") or "").strip()
        sku = str(raw.get("sku") or "").strip()
        status = str(raw.get("product_status") or "").upper()
        if not variant_id or not product_id:
            raise ReviewRequired("Shopify variant identity snapshot is invalid")
        if variant_id in current_by_variant:
            raise ReviewRequired(f"Shopify variant {variant_id} occurs more than once")
        row = {
            "id": variant_id,
            "product_id": product_id,
            "sku": sku,
            "product_status": status,
        }
        current_by_variant[variant_id] = row
        if sku:
            current_by_sku.setdefault(sku, []).append(row)
        elif status == "ACTIVE":
            hold(product_id, f"active variant {variant_id} has no SKU")

    for sku, rows in current_by_sku.items():
        if len(rows) > 1:
            owners = sorted({row["product_id"] for row in rows})
            reason = f"Shopify SKU {sku} is reused by products {owners}"
            for row in rows:
                hold(row["product_id"], reason)

    for mapped_product_id, mapping in mappings.items():
        if not isinstance(mapping, dict) or not isinstance(mapping.get("variants"), dict):
            raise ReviewRequired(f"mapping for Shopify product {mapped_product_id} is invalid")
        for variant_id, identity in mapping["variants"].items():
            if not isinstance(identity, dict):
                raise ReviewRequired(f"mapping for Shopify variant {variant_id} is invalid")
            expected_sku = str(identity.get("sku") or "").strip()
            if not expected_sku:
                raise ReviewRequired(f"mapping for Shopify variant {variant_id} has no SKU")
            current = current_by_variant.get(str(variant_id))
            if current is None:
                hold(
                    str(mapped_product_id),
                    f"mapped Shopify variant {variant_id} no longer exists",
                )
            elif (
                current["product_id"] != str(mapped_product_id)
                or current["sku"] != expected_sku
            ):
                hold(
                    str(mapped_product_id),
                    f"mapped Shopify variant {variant_id} changed from SKU {expected_sku}",
                )
                hold(
                    current["product_id"],
                    f"Shopify variant {variant_id} conflicts with a stored mapping",
                )
            for owner in current_by_sku.get(expected_sku, []):
                if (
                    owner["id"] != str(variant_id)
                    or owner["product_id"] != str(mapped_product_id)
                ):
                    reason = (
                        f"Shopify SKU {expected_sku} is already reserved by mapped "
                        f"variant {variant_id}"
                    )
                    hold(str(mapped_product_id), reason)
                    hold(owner["product_id"], reason)

    return {
        product_id: sorted(reasons)
        for product_id, reasons in sorted(holds.items())
    }


def discover_live_mappings(
    products: list[dict[str, Any]],
    items: list[dict[str, Any]],
    seller_id: str,
    site_id: str,
    discovered_at: str | None = None,
) -> dict[str, Any]:
    """Join Shopify variants to live seller items by one unambiguous exact SKU."""
    timestamp = discovered_at or utc_now()
    issues: list[dict[str, Any]] = []
    products_by_id: dict[str, dict[str, Any]] = {}
    shopify_by_sku: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        product_id = str(product.get("id") or "").strip()
        if not product_id or product_id in products_by_id:
            issues.append({"type": "shopify_product_id_invalid", "product_id": product_id})
            continue
        products_by_id[product_id] = product
        for variant in product.get("variants") or []:
            if not isinstance(variant, dict):
                issues.append({"type": "shopify_variant_invalid", "product_id": product_id})
                continue
            variant_id = str(variant.get("id") or "").strip()
            sku = str(variant.get("sku") or "").strip()
            if not variant_id:
                issues.append({"type": "shopify_variant_id_missing", "product_id": product_id})
                continue
            if not sku:
                issues.append(
                    {
                        "type": "shopify_sku_missing",
                        "product_id": product_id,
                        "variant_id": variant_id,
                    }
                )
                continue
            shopify_by_sku.setdefault(sku, []).append(
                {"product_id": product_id, "variant_id": variant_id, "variant": variant}
            )

    meli_by_sku: dict[str, list[dict[str, Any]]] = {}
    meli_rows_by_item: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        rows, item_issues = bootstrap_item_rows(item, seller_id, site_id)
        issues.extend(item_issues)
        if not rows:
            continue
        item_id = rows[0]["item_id"]
        meli_rows_by_item[item_id] = rows
        for row in rows:
            meli_by_sku.setdefault(row["sku"], []).append(row)

    links: list[dict[str, Any]] = []
    ambiguous_skus: list[dict[str, Any]] = []
    unmatched_shopify_skus: list[str] = []
    unmatched_meli_skus: list[str] = []
    for sku in sorted(set(shopify_by_sku) | set(meli_by_sku)):
        shopify_rows = shopify_by_sku.get(sku, [])
        meli_rows = meli_by_sku.get(sku, [])
        if len(shopify_rows) == 1 and len(meli_rows) == 1:
            links.append({"sku": sku, "shopify": shopify_rows[0], "meli": meli_rows[0]})
        elif len(shopify_rows) > 1 or (shopify_rows and len(meli_rows) > 1):
            ambiguity = {
                "type": "sku_match_ambiguous",
                "sku": sku,
                "shopify_variant_ids": [row["variant_id"] for row in shopify_rows],
                "shopify_product_ids": sorted(
                    {row["product_id"] for row in shopify_rows}
                ),
                "meli_item_ids": sorted({row["item_id"] for row in meli_rows}),
            }
            ambiguous_skus.append(ambiguity)
            issues.append(ambiguity)
        elif shopify_rows:
            unmatched_shopify_skus.append(sku)
        else:
            unmatched_meli_skus.append(sku)

    links_by_product: dict[str, list[dict[str, Any]]] = {}
    family_products: dict[str, set[str]] = {}
    for link in links:
        product_id = link["shopify"]["product_id"]
        links_by_product.setdefault(product_id, []).append(link)
        row = link["meli"]
        if not row["has_variations"] and row["family_id"]:
            family_products.setdefault(row["family_id"], set()).add(product_id)
    cross_product_families = {
        family_id: product_ids
        for family_id, product_ids in family_products.items()
        if len(product_ids) > 1
    }
    for family_id, product_ids in sorted(cross_product_families.items()):
        issues.append(
            {
                "type": "meli_family_matches_multiple_shopify_products",
                "family_id": family_id,
                "shopify_product_ids": sorted(product_ids),
            }
        )

    mappings: dict[str, dict[str, Any]] = {}
    for product_id, product_links in links_by_product.items():
        product = products_by_id[product_id]
        kinds = set()
        for link in product_links:
            row = link["meli"]
            if row["has_variations"]:
                kinds.add("legacy_variations")
            elif row["family_id"] and row["family_name"] and row["user_product_id"]:
                kinds.add("user_products_family")
            elif row["family_id"] or row["family_name"]:
                kinds.add("incomplete_user_product")
            else:
                # Legacy single-item listings can already have a user_product_id
                # even though they do not belong to the seller's family model.
                kinds.add("legacy_single")
        if len(kinds) != 1 or "incomplete_user_product" in kinds:
            issues.append(
                {
                    "type": "publication_model_ambiguous",
                    "product_id": product_id,
                    "models": sorted(kinds),
                }
            )
            continue

        variants = [value for value in product.get("variants") or [] if isinstance(value, dict)]
        variant_ids = {str(variant.get("id") or "") for variant in variants}
        mapped_variant_ids = {link["shopify"]["variant_id"] for link in product_links}
        kind = next(iter(kinds))
        family_id: str | None = None
        family_name: str | None = None
        if kind == "legacy_variations":
            item_ids = {link["meli"]["item_id"] for link in product_links}
            product_skus = {str(variant.get("sku") or "").strip() for variant in variants}
            if (
                len(item_ids) != 1
                or "" in product_skus
                or mapped_variant_ids != variant_ids
                or {row["sku"] for row in meli_rows_by_item[next(iter(item_ids))]}
                != product_skus
            ):
                issues.append(
                    {
                        "type": "legacy_item_not_complete_product",
                        "product_id": product_id,
                        "meli_item_ids": sorted(item_ids),
                    }
                )
                continue
        elif kind == "legacy_single":
            if len(variants) != 1 or len(product_links) != 1:
                issues.append(
                    {
                        "type": "separate_items_without_user_product_family",
                        "product_id": product_id,
                        "meli_item_ids": sorted(
                            {link["meli"]["item_id"] for link in product_links}
                        ),
                    }
                )
                continue
            kind = "legacy_variations"
        else:
            family_ids = {link["meli"]["family_id"] for link in product_links}
            family_names = {
                normalize_text(link["meli"]["family_name"]): link["meli"]["family_name"]
                for link in product_links
                if link["meli"]["family_name"]
            }
            user_product_ids = {link["meli"]["user_product_id"] for link in product_links}
            if (
                len(family_ids) != 1
                or next(iter(family_ids)) in cross_product_families
                or len(family_names) != 1
                or len(user_product_ids) != len(product_links)
            ):
                issues.append(
                    {
                        "type": "user_product_family_inconsistent",
                        "product_id": product_id,
                        "family_ids": sorted(family_ids),
                    }
                )
                continue
            family_id = next(iter(family_ids))
            family_name = next(iter(family_names.values()))

        mappings[product_id] = {
            "publication_model": kind,
            "family_name": family_name,
            "family_id": family_id,
            "variants": {
                link["shopify"]["variant_id"]: _bootstrap_variant_mapping(link)
                for link in product_links
            },
            "deferred_variant_ids": sorted(variant_ids - mapped_variant_ids),
            "synced_at": timestamp,
            "discovered_by": "live_exact_sku_bootstrap",
        }

    held_product_ids: set[str] = set()
    for issue in issues:
        product_id = str(issue.get("product_id") or "").strip()
        if product_id:
            held_product_ids.add(product_id)
        held_product_ids.update(
            str(value)
            for value in issue.get("shopify_product_ids") or []
            if str(value)
        )
        risky_skus = {
            str(value).strip()
            for value in issue.get("skus") or []
            if str(value).strip()
        }
        for field in ("sku", "seller_custom_field", "seller_sku"):
            value = str(issue.get(field) or "").strip()
            if value:
                risky_skus.add(value)
        for sku in risky_skus:
            held_product_ids.update(
                row["product_id"] for row in shopify_by_sku.get(sku, [])
            )

    unidentified_active_item_ids = sorted(
        {
            str(issue.get("item_id"))
            for issue in issues
            if issue.get("type")
            in {"meli_variations_invalid", "meli_sku_row_invalid", "meli_sku_missing"}
            and issue.get("status") in {"active", "paused"}
            and issue.get("item_id")
        }
    )

    return {
        "mappings": mappings,
        "issues": issues,
        "matched_skus": len(links),
        "held_product_ids": sorted(held_product_ids),
        "unidentified_active_item_ids": unidentified_active_item_ids,
        "ambiguous_skus": ambiguous_skus,
        "unmatched_shopify_skus": unmatched_shopify_skus,
        "unmatched_meli_skus": unmatched_meli_skus,
    }


def candidate_sku_rows(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return item_sku_rows(candidate["body"])


def item_match_errors(
    candidate: dict[str, Any], item: dict[str, Any], seller_id: str, require_active: bool
) -> list[str]:
    body = candidate["body"]
    errors: list[str] = []
    if str(item.get("seller_id") or "") != seller_id:
        errors.append("seller differs")
    if str(item.get("category_id") or "") != str(body.get("category_id") or ""):
        errors.append("category differs")
    if candidate["publication_model"] == "legacy_variations":
        if normalize_text(item.get("title")) != normalize_text(body.get("title")):
            errors.append("title differs")
    elif normalize_text(item.get("family_name")) != normalize_text(body.get("family_name")):
        errors.append("family_name differs")

    expected_variations = body.get("variations") or []
    actual_variations = item.get("variations") or []
    if len(actual_variations) != len(expected_variations):
        errors.append("variation count differs")
    for index, variation in enumerate(actual_variations):
        if not isinstance(variation, dict):
            errors.append(f"variation {index} is invalid")
            continue
        custom_sku = str(variation.get("seller_custom_field") or "").strip()
        seller_sku = attribute_value(variation.get("attributes"), "SELLER_SKU")
        if not custom_sku or not seller_sku:
            errors.append(f"variation {index} is missing an exact SKU field")

    expected_rows = candidate_sku_rows(candidate)
    actual_rows = item_sku_rows(item)
    if set(expected_rows) != set(actual_rows):
        errors.append("SKU set differs")
    else:
        for sku, expected in expected_rows.items():
            actual = actual_rows[sku]
            if actual.get("duplicate"):
                errors.append(f"SKU {sku} occurs more than once")
            for field_name, label in (
                ("custom_sku", "seller_custom_field"),
                ("seller_sku", "SELLER_SKU"),
            ):
                if (
                    expected.get(field_name)
                    and actual.get(field_name) != expected.get(field_name)
                ):
                    errors.append(f"{label} differs for SKU {sku}")
            try:
                if Decimal(str(actual.get("price"))) != Decimal(str(expected.get("price"))):
                    errors.append(f"price differs for SKU {sku}")
            except (InvalidOperation, TypeError, ValueError):
                errors.append(f"price is invalid for SKU {sku}")
            if actual.get("quantity") != expected.get("quantity"):
                errors.append(f"stock differs for SKU {sku}")
    if len(item.get("pictures") or []) < len(body.get("pictures") or []):
        errors.append("picture count is incomplete")
    if require_active:
        if item.get("status") != "active":
            errors.append(f"status is {item.get('status')}")
        if item.get("sub_status"):
            errors.append(f"sub_status is {item.get('sub_status')}")
    return errors


def payload_hash(candidates: list[dict[str, Any]]) -> str:
    normalized = [
        {"key": candidate["key"], "body": candidate["body"], "description": candidate["description"]}
        for candidate in candidates
    ]
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def existing_candidate_item(
    candidate: dict[str, Any],
    meli: MeliClient,
    seller_id: str,
    recovering_create: bool = False,
) -> dict[str, Any] | None:
    matches_by_sku = {
        sku: set(meli.search_exact_sku(sku)) for sku in candidate.get("skus") or []
    }
    all_ids = set().union(*matches_by_sku.values()) if matches_by_sku else set()
    if not all_ids:
        return None
    if len(all_ids) != 1 or any(ids != all_ids for ids in matches_by_sku.values()):
        raise ReviewRequired(
            f"exact SKU search is ambiguous for {candidate.get('skus')}: {sorted(all_ids)}"
        )
    item_id = next(iter(all_ids))
    item = meli.get_item(item_id)
    if item is None:
        raise SyncError(f"exact-SKU item {item_id} disappeared during reconciliation")
    errors = item_match_errors(
        candidate,
        item,
        seller_id,
        require_active=not recovering_create,
    )
    if not recovering_create:
        description = meli.get_description(item_id)
        if normalize_text(description) != normalize_text(candidate["description"]):
            errors.append("description differs")
    if errors:
        raise ReviewRequired(
            f"existing exact-SKU item {item_id} does not match the proposal: {errors}"
        )
    return item


def wait_for_item(
    candidate: dict[str, Any], item_id: str, meli: MeliClient, seller_id: str,
    attempts: int, delay: float,
) -> dict[str, Any]:
    last_item: dict[str, Any] | None = None
    last_errors: list[str] = []
    for attempt in range(attempts):
        last_item = meli.get_item(item_id)
        if last_item is None:
            last_errors = ["item is missing"]
        else:
            last_errors = item_match_errors(candidate, last_item, seller_id, require_active=True)
            description = meli.get_description(item_id)
            if normalize_text(description) != normalize_text(candidate["description"]):
                last_errors.append("description differs")
        if not last_errors:
            return last_item
        if attempt + 1 < attempts and delay:
            time.sleep(delay)
    raise SyncError(f"item {item_id} is not yet verified: {last_errors}")


def emit(value: dict[str, Any], output: Path | None = None) -> None:
    if output is not None:
        atomic_json(output, value, mode=0o600)
    print(json.dumps(value, ensure_ascii=False))


def command_bootstrap(args: argparse.Namespace, settings: Settings) -> int:
    store = StateStore(_path(args.state))
    with exclusive_lock(_path(args.lock)), stock_worker_lock(settings.stock_worker_lock_file):
        state = store.load()
        if args.apply and state.get("pending") is not None:
            raise ReviewRequired("cannot apply bootstrap while a product publication is pending")

        shopify = ShopifyClient(settings)
        meli = MeliClient(settings)
        seller = meli.verify_seller()

        products: list[dict[str, Any]] = []
        for ref in shopify.all_product_refs():
            product_id = str(ref.get("id") or "")
            product = shopify.get_product(product_id)
            if product is None:
                raise SyncError(f"Shopify product {product_id} disappeared during bootstrap")
            products.append(product)

        items: list[dict[str, Any]] = []
        disappeared_item_ids: list[str] = []
        for item_id in meli.all_seller_item_ids():
            item = meli.get_item(item_id)
            if item is None:
                disappeared_item_ids.append(item_id)
                continue
            if not item.get("id"):
                item = {**item, "id": item_id}
            items.append(item)

        discovery = discover_live_mappings(
            products,
            items,
            str(seller.get("id") or ""),
            settings.meli_site_id,
        )
        for item_id in disappeared_item_ids:
            discovery["issues"].append(
                {"type": "meli_item_disappeared_during_scan", "item_id": item_id}
            )

        new_mappings: dict[str, dict[str, Any]] = {}
        preserved_existing: list[str] = []
        blocked_conflicts: list[str] = []
        held_product_ids = set(discovery["held_product_ids"])
        for product_id, mapping in discovery["mappings"].items():
            if product_id in held_product_ids:
                continue
            if product_id in state["mappings"]:
                preserved_existing.append(product_id)
                continue
            if product_id in state["blocked"]:
                blocked_conflicts.append(product_id)
                discovery["issues"].append(
                    {
                        "type": "discovered_product_is_locally_blocked",
                        "product_id": product_id,
                    }
                )
                continue
            new_mappings[product_id] = mapping

        completed_at = utc_now()
        marker = {
            "completed_at": completed_at,
            "seller_id": str(seller.get("id") or ""),
            "shopify_products_scanned": len(products),
            "meli_items_scanned": len(items) + len(disappeared_item_ids),
            "matched_skus": discovery["matched_skus"],
            "mappings_added": len(new_mappings),
            "review_issue_count": len(discovery["issues"]),
            "held_product_ids": discovery["held_product_ids"],
        }
        application_blocking_item_ids = sorted(
            set(discovery["unidentified_active_item_ids"]) | set(disappeared_item_ids)
        )
        applied = bool(args.apply and not application_blocking_item_ids)
        if applied:
            state["mappings"].update(new_mappings)
            state["bootstrap"] = marker
            store.save(state)

        outcome = (
            "bootstrap_preview"
            if not args.apply
            else "bootstrap_applied"
            if applied
            else "bootstrap_not_applied"
        )
        report = {
            "outcome": outcome,
            "marketplace_writes": False,
            "state_written": applied,
            **marker,
            "application_blocking_item_ids": application_blocking_item_ids,
            "preserved_existing_product_ids": sorted(preserved_existing),
            "blocked_product_ids_not_mapped": sorted(blocked_conflicts),
            "discovered_mappings": new_mappings,
            "ambiguous_skus": discovery["ambiguous_skus"],
            "unmatched_shopify_skus": discovery["unmatched_shopify_skus"],
            "unmatched_meli_skus": discovery["unmatched_meli_skus"],
            "issues": discovery["issues"],
        }
        atomic_json(_path(args.result), report)
        emit(
            {
                "outcome": report["outcome"],
                **marker,
                "state_written": applied,
                "application_blocking_item_ids": application_blocking_item_ids,
                "report": str(_path(args.result)),
            }
        )
        return 2 if args.apply and not applied else 0


def command_next(args: argparse.Namespace, settings: Settings) -> int:
    store = StateStore(_path(args.state))
    with exclusive_lock(_path(args.lock)), stock_worker_lock(settings.stock_worker_lock_file):
        state = store.load()
        if not args.dry_run and not isinstance(state.get("bootstrap"), dict):
            raise ReviewRequired(
                "live SKU bootstrap must be applied before the first production cycle"
            )
        if args.dry_run:
            state = json.loads(json.dumps(state))
        shopify = ShopifyClient(settings)
        meli = MeliClient(settings)
        seller = meli.verify_seller()
        supports_user_products = "user_product_seller" in [str(tag) for tag in seller.get("tags") or []]
        refs = shopify.active_product_refs()
        active_ids = {str(ref["id"]) for ref in refs}
        sku_holds = current_sku_holds(shopify.all_variant_refs(), state["mappings"])
        held_product_ids = set(sku_holds) | {
            str(value)
            for value in (state.get("bootstrap") or {}).get("held_product_ids") or []
        }

        product: dict[str, Any] | None = None
        model: str | None = None
        targets: list[str] = []
        resumed = False
        pending = state.get("pending")
        if isinstance(pending, dict):
            pending_id = str(pending.get("shopify_id") or "")
            pending_model = str(pending.get("publication_model") or "")
            if pending_model not in {"legacy_variations", "user_products_family"}:
                raise ReviewRequired("pending publication model is invalid")
            if pending_id in active_ids:
                pending_product = shopify.get_product(pending_id)
            else:
                pending_product = None
            if pending_product is None or str(pending_product.get("status") or "").upper() != "ACTIVE":
                if pending.get("items") or pending.get("creating_key"):
                    raise ReviewRequired(
                        f"pending Shopify product {pending_id} is no longer active or accessible"
                    )
                state["pending"] = None
                if not args.dry_run:
                    store.save(state)
            else:
                pending_mapping = state["mappings"].get(pending_id)
                if pending.get("items") or pending.get("creating_key"):
                    refreshed_targets = [
                        str(value) for value in pending.get("target_variant_ids") or []
                    ]
                    if not refreshed_targets:
                        raise ReviewRequired(
                            "pending product with creation history has no target variants"
                        )
                else:
                    refreshed_targets = target_variant_ids(
                        pending_product,
                        pending_mapping,
                        pending_model,
                    )
                    if refreshed_targets:
                        pending["target_variant_ids"] = refreshed_targets
                        for transient_key in ("payload_hash", "stage", "creating_key"):
                            pending.pop(transient_key, None)
                    else:
                        state["pending"] = None
                    if not args.dry_run:
                        store.save(state)
                if refreshed_targets:
                    product = pending_product
                    model = pending_model
                    targets = refreshed_targets
                    resumed = True

        if product is None:
            mappings = state.get("mappings") or {}
            blocked = state.get("blocked") or {}
            for ref in refs:
                shopify_id = str(ref["id"])
                if shopify_id in blocked or shopify_id in held_product_ids:
                    continue
                mapping = mappings.get(shopify_id)
                candidate_model = (
                    str(mapping.get("publication_model"))
                    if isinstance(mapping, dict) and mapping.get("publication_model")
                    else (
                        "user_products_family"
                        if supports_user_products
                        else "legacy_variations"
                    )
                )
                if mapping is not None and candidate_model != "user_products_family":
                    continue
                candidate = shopify.get_product(shopify_id)
                if candidate is None or not any(
                    sellable(variant) for variant in candidate.get("variants") or []
                ):
                    continue
                candidate_targets = target_variant_ids(candidate, mapping, candidate_model)
                if candidate_targets:
                    product = candidate
                    model = candidate_model
                    targets = candidate_targets
                    break

        if product is None:
            source = {
                "generated_at": utc_now(),
                "dry_run": bool(args.dry_run),
                "product": None,
                "sku_holds": sku_holds,
                "held_product_ids": sorted(held_product_ids),
            }
            atomic_json(_path(args.output), source)
            emit(
                {
                    "outcome": "no-op",
                    "reason": "no eligible unsynced product",
                    "sku_holds": sku_holds,
                    "held_product_ids": sorted(held_product_ids),
                }
            )
            return 0

        resume = {
            "resumed": resumed,
            "stage": None,
            "meli_item_ids": [],
            "reuse_existing_payload": False,
        }
        if resumed and isinstance(pending, dict):
            resume["stage"] = pending.get("stage")
            resume["meli_item_ids"] = sorted(
                {str(value) for value in (pending.get("items") or {}).values()}
            )
            resume["reuse_existing_payload"] = bool(resume["meli_item_ids"])

        mapping = state["mappings"].get(str(product["id"]))
        categories = meli.discover_categories(str(product.get("title") or ""))
        attributes = (
            meli.category_attributes(str(categories[0]["category_id"])) if categories else []
        )
        source = {
            "generated_at": utc_now(),
            "dry_run": bool(args.dry_run),
            "seller_id": str(seller.get("id")),
            "seller_tags": [str(tag) for tag in seller.get("tags") or []],
            "publication_model": model,
            "target_variant_ids": targets,
            "existing_mapping": mapping,
            "resume": resume,
            "product_fingerprint": source_fingerprint(product),
            "product": product,
            "issues": product_issues(product),
            "category_candidates": categories,
            "initial_category_attributes": attributes,
            "sku_holds": sku_holds,
            "held_product_ids": sorted(held_product_ids),
        }
        if not args.dry_run:
            if not resumed:
                state["pending"] = {
                    "shopify_id": str(product["id"]),
                    "publication_model": model,
                    "target_variant_ids": targets,
                    "selected_at": utc_now(),
                    "items": {},
                    "created_keys": [],
                }
                store.save(state)
        atomic_json(_path(args.output), source)
        emit(
            {
                "outcome": "prepared",
                "shopify_id": str(product["id"]),
                "publication_model": model,
                "target_variant_ids": targets,
                "issues": source["issues"],
                "resumed": resumed,
                "dry_run": bool(args.dry_run),
                "source": str(_path(args.output)),
            }
        )
        return 0


def command_category(args: argparse.Namespace, settings: Settings) -> int:
    with stock_worker_lock(settings.stock_worker_lock_file):
        meli = MeliClient(settings)
        meli.verify_seller()
        if args.query:
            result = {"query": args.query, "candidates": meli.discover_categories(args.query)}
        else:
            result = {
                "category_id": args.category_id,
                "attributes": meli.category_attributes(args.category_id),
            }
    emit(result, _path(args.output))
    return 0


def command_block(args: argparse.Namespace) -> int:
    store = StateStore(_path(args.state))
    with exclusive_lock(_path(args.lock)):
        state = store.load()
        pending = state.get("pending")
        if not isinstance(pending, dict) or not pending.get("shopify_id"):
            raise ReviewRequired("there is no pending product to block")
        if pending.get("items"):
            raise ReviewRequired("a product with created Mercado Libre items cannot be auto-blocked")
        if pending.get("creating_key"):
            raise ReviewRequired("creation outcome is uncertain; do not block without manual reconciliation")
        shopify_id = str(pending["shopify_id"])
        state["blocked"][shopify_id] = {"reason": args.reason, "blocked_at": utc_now()}
        state["pending"] = None
        store.save(state)
        emit({"outcome": "blocked", "shopify_id": shopify_id, "reason": args.reason})
        return 0


def _pause_created_family_items(pending: dict[str, Any], meli: MeliClient) -> None:
    created_keys = {str(value) for value in pending.get("created_keys") or []}
    for key, item_id in (pending.get("items") or {}).items():
        if str(key) not in created_keys:
            continue
        try:
            meli.set_status(str(item_id), "paused")
        except SyncError:
            pass


def command_publish(args: argparse.Namespace, settings: Settings) -> int:
    store = StateStore(_path(args.state))
    result_path = _path(args.result)
    with exclusive_lock(_path(args.lock)), stock_worker_lock(settings.stock_worker_lock_file):
        state = store.load()
        source = read_json(_path(args.source), None)
        payload = read_json(_path(args.payload), None)
        if not isinstance(source, dict) or not isinstance(payload, dict):
            raise ReviewRequired("source.json and payload.json must contain JSON objects")
        if bool(source.get("dry_run")) != bool(args.dry_run):
            raise ReviewRequired("next and publish must use the same dry-run mode")

        if not args.dry_run:
            pending = state.get("pending")
            if not isinstance(pending, dict) or str(
                pending.get("shopify_id") or ""
            ) != str(payload.get("shopify_id") or ""):
                raise ReviewRequired("production publish requires the current pending Shopify product")
        else:
            pending = {
                "shopify_id": str(payload.get("shopify_id") or ""),
                "publication_model": source.get("publication_model"),
                "target_variant_ids": source.get("target_variant_ids") or [],
                "items": {},
            }

        shopify = ShopifyClient(settings)
        current_product = shopify.get_product(str(payload.get("shopify_id") or ""))
        if current_product is None or str(current_product.get("status") or "").upper() != "ACTIVE":
            raise ReviewRequired("the pending Shopify product is no longer active")
        if source_fingerprint(current_product) != str(source.get("product_fingerprint") or ""):
            raise ValidationFailed(
                "Shopify product changed after preparation; run next again and rebuild payload.json"
            )
        sku_holds = current_sku_holds(shopify.all_variant_refs(), state["mappings"])
        shopify_id = str(payload.get("shopify_id") or "")
        if shopify_id in sku_holds:
            raise ValidationFailed(
                "Shopify SKU ownership changed or is ambiguous: "
                + "; ".join(sku_holds[shopify_id])
            )

        meli = MeliClient(settings)
        seller = meli.verify_seller()
        current_model = (
            "user_products_family"
            if "user_product_seller" in [str(tag) for tag in seller.get("tags") or []]
            else "legacy_variations"
        )
        mapping = state["mappings"].get(str(payload.get("shopify_id") or ""))
        expected_model = (
            str(mapping.get("publication_model"))
            if isinstance(mapping, dict) and mapping.get("publication_model")
            else current_model
        )
        if expected_model != source.get("publication_model"):
            raise ValidationFailed("Mercado Libre seller publication model changed; run next again")

        candidates = build_candidates(source, payload, settings.listing_type_id)
        validations = [
            {"key": candidate["key"], **meli.validate_item(candidate["body"])}
            for candidate in candidates
        ]
        if not all(validation.get("valid") for validation in validations):
            result = {
                "outcome": "validation_failed",
                "shopify_id": str(payload.get("shopify_id")),
                "validations": validations,
            }
            emit(result, result_path)
            return 2
        if args.dry_run:
            emit(
                {
                    "outcome": "validated",
                    "shopify_id": str(payload.get("shopify_id")),
                    "publication_model": source.get("publication_model"),
                    "item_count": len(candidates),
                    "validations": validations,
                },
                result_path,
            )
            return 0

        digest = payload_hash(candidates)
        existing_digest = pending.get("payload_hash")
        if existing_digest and existing_digest != digest and pending.get("items"):
            raise ReviewRequired("payload changed after an item was created; manual review is required")
        pending["payload_hash"] = digest
        pending["stage"] = "validated"
        state["pending"] = pending
        store.save(state)

        try:
            for candidate in candidates:
                key = str(candidate["key"])
                item_id = str((pending.get("items") or {}).get(key) or "")
                created_keys = {str(value) for value in pending.get("created_keys") or []}
                if item_id:
                    item = meli.get_item(item_id)
                    if item is None:
                        raise ReviewRequired(f"pending Mercado Libre item {item_id} disappeared")
                    errors = item_match_errors(
                        candidate,
                        item,
                        settings.meli_seller_id,
                        require_active=False,
                    )
                    if errors:
                        raise ReviewRequired(
                            f"pending Mercado Libre item {item_id} changed: {errors}"
                        )
                else:
                    recovering_create = str(pending.get("creating_key") or "") == key
                    item = existing_candidate_item(
                        candidate,
                        meli,
                        settings.meli_seller_id,
                        recovering_create=recovering_create,
                    )
                    if item is None:
                        if recovering_create:
                            raise SyncError(
                                "a previous create may still be indexing; refusing a "
                                "duplicate until exact-SKU reconciliation succeeds"
                            )
                        pending["stage"] = "creating"
                        pending["creating_key"] = key
                        state["pending"] = pending
                        store.save(state)
                        try:
                            item = meli.create_item(candidate["body"])
                        except CreateRejected:
                            pending.pop("creating_key", None)
                            pending["stage"] = "validated"
                            state["pending"] = pending
                            store.save(state)
                            raise
                        created_keys.add(key)
                    elif recovering_create:
                        created_keys.add(key)
                    item_id = str(item["id"])
                    pending.setdefault("items", {})[key] = item_id
                    pending["created_keys"] = sorted(created_keys)
                    pending.pop("creating_key", None)
                    pending["stage"] = "created"
                    state["pending"] = pending
                    store.save(state)
                if (
                    source.get("publication_model") == "user_products_family"
                    and key in created_keys
                ):
                    meli.set_status(item_id, "paused")
            for candidate in candidates:
                key = str(candidate["key"])
                if key in {str(value) for value in pending.get("created_keys") or []}:
                    item_id = str(pending["items"][key])
                    meli.set_description(item_id, candidate["description"])
            if source.get("publication_model") == "user_products_family":
                for key in pending.get("created_keys") or []:
                    item_id = (pending.get("items") or {}).get(str(key))
                    if item_id:
                        meli.set_status(str(item_id), "active")

            verified: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                key = str(candidate["key"])
                verified[key] = wait_for_item(
                    candidate,
                    str(pending["items"][key]),
                    meli,
                    settings.meli_seller_id,
                    attempts=args.poll_attempts,
                    delay=args.poll_delay,
                )
        except Exception:
            if source.get("publication_model") == "user_products_family":
                _pause_created_family_items(pending, meli)
            raise

        shopify_id = str(payload["shopify_id"])
        previous = state["mappings"].get(shopify_id)
        variant_mapping = (
            dict(previous.get("variants") or {}) if isinstance(previous, dict) else {}
        )
        for candidate in candidates:
            key = str(candidate["key"])
            item_id = str(pending["items"][key])
            live_rows = item_sku_rows(verified[key])
            for variant_id in candidate["variant_ids"]:
                variant = _variant_by_id(source["product"])[variant_id]
                row = live_rows[str(variant["sku"])]
                variant_mapping[variant_id] = {
                    "sku": str(variant["sku"]),
                    "meli_item_id": item_id,
                    "meli_variation_id": (
                        str(row["variation_id"]) if row.get("variation_id") is not None else None
                    ),
                    "user_product_id": verified[key].get("user_product_id"),
                }
        family_ids = {
            str(item.get("family_id"))
            for item in verified.values()
            if item.get("family_id") is not None
        }
        if source.get("publication_model") == "user_products_family":
            prior_family = (
                str(previous.get("family_id"))
                if isinstance(previous, dict) and previous.get("family_id") is not None
                else ""
            )
            if prior_family:
                family_ids.add(prior_family)
            user_product_ids = {
                str(item.get("user_product_id") or "") for item in verified.values()
            }
            if isinstance(previous, dict):
                user_product_ids.update(
                    str(value.get("user_product_id") or "")
                    for value in (previous.get("variants") or {}).values()
                    if isinstance(value, dict)
                )
            expected_user_products = len(verified) + (
                len(previous.get("variants") or {}) if isinstance(previous, dict) else 0
            )
            if len(family_ids) != 1 or "" in user_product_ids or len(user_product_ids) != expected_user_products:
                _pause_created_family_items(pending, meli)
                raise ReviewRequired(
                    "User Product items did not resolve to one family with distinct product IDs"
                )

        all_variant_ids = {str(variant["id"]) for variant in source["product"].get("variants") or []}
        mapping = {
            "publication_model": source.get("publication_model"),
            "family_name": (
                payload.get("family_name")
                if source.get("publication_model") == "user_products_family"
                else None
            ),
            "family_id": next(iter(family_ids)) if family_ids else None,
            "variants": variant_mapping,
            "deferred_variant_ids": sorted(all_variant_ids - set(variant_mapping)),
            "synced_at": utc_now(),
        }
        state["mappings"][shopify_id] = mapping
        state["pending"] = None
        store.save(state)
        emit(
            {
                "outcome": "published",
                "shopify_id": shopify_id,
                "publication_model": source.get("publication_model"),
                "meli_item_ids": list(dict.fromkeys(str(value) for value in pending["items"].values())),
                "mapping": mapping,
                "validations": validations,
            },
            result_path,
        )
        return 0


def command_status(args: argparse.Namespace) -> int:
    state = StateStore(_path(args.state)).load()
    emit(
        {
            "outcome": "status",
            "mapped_products": len(state["mappings"]),
            "blocked_products": len(state["blocked"]),
            "pending": state.get("pending"),
            "bootstrap": state.get("bootstrap"),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=os.environ.get("PRODUCT_SYNC_STATE_FILE", str(DEFAULT_STATE)))
    parser.add_argument("--lock", default=str(DEFAULT_LOCK))
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Discover existing Shopify-to-Mercado-Libre SKU mappings"
    )
    bootstrap_parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically add verified mappings and mark bootstrap complete",
    )
    bootstrap_parser.add_argument("--result", default=str(DEFAULT_BOOTSTRAP))

    next_parser = subparsers.add_parser("next", help="Select or resume exactly one Shopify product")
    next_parser.add_argument("--dry-run", action="store_true")
    next_parser.add_argument("--output", default=str(DEFAULT_SOURCE))

    category_parser = subparsers.add_parser("category", help="Read Mercado Libre category information")
    category_group = category_parser.add_mutually_exclusive_group(required=True)
    category_group.add_argument("--query")
    category_group.add_argument("--id", dest="category_id")
    category_parser.add_argument("--output", default=str(DEFAULT_CATEGORY))

    publish_parser = subparsers.add_parser("publish", help="Validate and publish the prepared product")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    publish_parser.add_argument("--payload", default=str(DEFAULT_PAYLOAD))
    publish_parser.add_argument("--result", default=str(DEFAULT_RESULT))
    publish_parser.add_argument("--poll-attempts", type=int, default=6)
    publish_parser.add_argument("--poll-delay", type=float, default=10)

    block_parser = subparsers.add_parser("block", help="Block the current product before creation")
    block_parser.add_argument("--reason", required=True)

    subparsers.add_parser("status", help="Show local migration progress without API calls")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return command_status(args)
        if args.command == "block":
            return command_block(args)
        settings = Settings.from_env()
        if args.command == "next":
            return command_next(args, settings)
        if args.command == "bootstrap":
            return command_bootstrap(args, settings)
        if args.command == "category":
            return command_category(args, settings)
        if args.command == "publish":
            return command_publish(args, settings)
        parser.error("unknown command")
    except SyncError as error:
        result = {"outcome": error.outcome, "reason": str(error)}
        if error.details is not None:
            result["details"] = error.details
        result_path = _path(getattr(args, "result", DEFAULT_RESULT))
        emit(result, result_path)
        return 4 if error.outcome == "failed_retryable" else 3
    except Exception as error:  # Keep credentials and HTTP bodies out of scheduler logs.
        result = {
            "outcome": "failed_retryable",
            "reason": f"unexpected internal error: {type(error).__name__}",
        }
        result_path = _path(getattr(args, "result", DEFAULT_RESULT))
        emit(result, result_path)
        return 4
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
