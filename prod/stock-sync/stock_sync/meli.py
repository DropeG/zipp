from __future__ import annotations

import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import Settings
from .errors import RetryableSyncError, ReviewRequiredError
from .models import MeliListing, MeliOrder, MeliOrderLine


@contextmanager
def _review_errors(key: str, operation: str):
    try:
        yield
    except ReviewRequiredError as error:
        raise ReviewRequiredError(key, str(error), error.details) from error
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ReviewRequiredError(
            key, f"Mercado Libre {operation}: invalid or incomplete {error}", {"operation": operation}
        ) from error


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("object")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("array")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("text")
    return value


def _id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError("identifier")
    return str(value)


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("quantity or pagination count")
    return value


class RESTExecutor(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...


class MeliTransport:
    """Authenticated REST transport. The worker owns retries for temporary errors."""

    def __init__(
        self, settings: Settings, tokens_file: str | Path | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.tokens_file = Path(tokens_file if tokens_file is not None else os.environ.get(
            "MELI_TOKENS_FILE", "prod/stock-sync/data/meli_tokens.json"
        ))
        self.session = session or requests.Session()

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with _review_errors(path, path):
            tokens = self._load_tokens()
            refreshed = False
            if time.time() >= tokens["expires_at"] - 120:
                tokens = self._refresh(tokens)
                refreshed = True
            response = self._send(method, path, tokens["access_token"], **kwargs)
            if response.status_code == 401 and not refreshed:
                tokens = self._refresh(tokens)
                response = self._send(method, path, tokens["access_token"], **kwargs)
            return self._payload(response, path)

    def _load_tokens(self) -> dict[str, Any]:
        try:
            tokens = _object(json.loads(self.tokens_file.read_text()))
            _text(tokens["access_token"])
            _text(tokens["refresh_token"])
            expiry = tokens["expires_at"]
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise ValueError("token expiry")
            return tokens
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ReviewRequiredError("meli-auth", "Mercado Libre token file is missing or invalid", {}) from error

    def _send(self, method: str, path: str, access_token: str | None = None, **kwargs: Any):
        headers = {"Accept": "application/json"}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            return self.session.request(
                method, f"https://api.mercadolibre.com{path}", headers=headers, timeout=30, **kwargs
            )
        except requests.RequestException as error:
            raise RetryableSyncError(f"Mercado Libre {method} {path} network failure") from error

    @staticmethod
    def _payload(response, path: str) -> dict[str, Any]:
        status = response.status_code
        if status == 429 or status >= 500:
            raise RetryableSyncError(f"Mercado Libre {path} failed with HTTP {status}")
        if not 200 <= status < 300:
            raise ReviewRequiredError(path, f"Mercado Libre {path} failed with HTTP {status}", {"status": status})
        try:
            return _object(response.json(parse_float=Decimal))
        except (ValueError, TypeError) as error:
            raise ReviewRequiredError(path, f"Mercado Libre {path} returned malformed JSON", {}) from error

    def _refresh(self, tokens: dict[str, Any]) -> dict[str, Any]:
        response = self._send("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "client_id": self.settings.meli_app_id,
            "client_secret": self.settings.meli_client_secret, "refresh_token": tokens["refresh_token"],
        })
        new_tokens = self._payload(response, "/oauth/token")
        _text(new_tokens["access_token"])
        _text(new_tokens["refresh_token"])
        expires_in = _integer(new_tokens["expires_in"], 1)
        if "user_id" in new_tokens and _id(new_tokens["user_id"]) != self.settings.meli_expected_seller_id:
            raise ValueError("token seller")
        new_tokens["expires_at"] = time.time() + expires_in
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.tokens_file.parent, prefix=".meli-tokens-", delete=False) as output:
                temporary = Path(output.name)
                json.dump(new_tokens, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.tokens_file)
        except OSError as error:
            raise ReviewRequiredError("meli-auth", "Mercado Libre refreshed tokens could not be saved", {}) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return new_tokens


class MeliClient:
    def __init__(
        self, settings: Settings, transport: RESTExecutor | None = None,
        tokens_file: str | Path | None = None,
    ) -> None:
        self.seller_id = settings.meli_expected_seller_id
        self.transport = transport or MeliTransport(settings, tokens_file=tokens_file)

    def verify_seller(self) -> str:
        with _review_errors("meli-seller", "verify_seller"):
            seller_id = _id(self.transport.request("GET", "/users/me")["id"])
            self._check_seller(seller_id)
            return seller_id

    def get_order(self, order_id: str) -> MeliOrder:
        with _review_errors(f"order:{order_id}", "get_order"):
            data = self.transport.request("GET", f"/orders/{quote(_id(order_id), safe='')}")
            seller_id = _id(_object(data["seller"])["id"])
            self._check_seller(seller_id)
            if _id(data["id"]) != str(order_id):
                raise ValueError("order identifier")
            processed_at = _text(data.get("date_closed") or data["date_created"])
            if datetime.fromisoformat(processed_at).tzinfo is None:
                raise ValueError("order date timezone")
            status = _text(data["status"])
            lines = []
            items: dict[str, dict[str, Any]] = {}
            for raw in _array(data["order_items"]):
                raw = _object(raw)
                entity = _object(raw["item"])
                item_id = _id(entity["id"])
                variation_id = None if entity.get("variation_id") is None else _id(entity["variation_id"])
                sku = self._sku(entity)
                if not sku:
                    if item_id not in items:
                        items[item_id] = self._get_item(item_id)
                    detail = items[item_id]
                    if variation_id is not None:
                        matches = [v for v in self._variations(detail) if _id(v["id"]) == variation_id]
                        sku = self._sku(matches[0]) if matches else ""
                    elif not self._variations(detail):
                        sku = self._sku(detail)
                price = str(raw["unit_price"])
                amount = Decimal(price)
                if not amount.is_finite() or amount < 0:
                    raise ValueError("unit price")
                lines.append(MeliOrderLine(
                    item_id, variation_id, _text(entity["title"]), _integer(raw["quantity"], 1),
                    price, _text(raw.get("currency_id", data.get("currency_id"))), sku,
                ))
            if not lines:
                raise ValueError("order lines")
            return MeliOrder(str(order_id), seller_id, status, processed_at, lines)

    def search_paid_orders(self, since: str | datetime) -> list[str]:
        with _review_errors("meli-orders", "search_paid_orders"):
            since_text = since.isoformat() if isinstance(since, datetime) else _text(since)
            if datetime.fromisoformat(since_text).tzinfo is None:
                raise ValueError("search date timezone")
            offset = 0
            order_ids: dict[str, None] = {}
            while True:
                data = self.transport.request("GET", "/orders/search", params={
                    "seller": self.seller_id, "order.status": "paid", "order.date_created.from": since_text,
                    "sort": "date_asc", "limit": 50, "offset": offset,
                })
                results, total = self._page(data)
                if _integer(data["paging"]["offset"]) != offset:
                    raise ValueError("order pagination offset")
                for result in results:
                    order_ids[_id(_object(result)["id"])] = None
                offset += len(results)
                if offset >= total:
                    return list(order_ids)
                if not results:
                    raise ValueError("incomplete order pagination")

    def list_all_listings(self) -> list[MeliListing]:
        with _review_errors("meli-listings", "list_all_listings"):
            item_ids: dict[str, None] = {}
            params: dict[str, Any] = {"search_type": "scan", "limit": 100}
            while True:
                data = self.transport.request("GET", f"/users/{quote(self.seller_id, safe='')}/items/search", params=dict(params))
                results, total = self._page(data)
                for raw_id in results:
                    item_id = _id(raw_id)
                    if item_id in item_ids:
                        raise ValueError("repeated listing in scan pagination")
                    item_ids[item_id] = None
                if len(item_ids) >= total:
                    break
                if not results:
                    raise ValueError("incomplete listing pagination")
                params["scroll_id"] = _text(data["scroll_id"])

            listings = []
            for item_id in item_ids:
                detail = self._get_item(item_id)
                variations = self._variations(detail)
                if variations:
                    for variation in variations:
                        listings.append(MeliListing(item_id, _id(variation["id"]), self._sku(variation),
                                                    _integer(variation["available_quantity"])))
                else:
                    listings.append(MeliListing(item_id, None, self._sku(detail), _integer(detail["available_quantity"])))
            return listings

    def set_available_quantity(self, listing: MeliListing, quantity: int) -> None:
        with _review_errors(f"sku:{listing.sku}", "set_available_quantity"):
            if type(quantity) is not int:
                raise ValueError("quantity")
            quantity = max(0, quantity)
            item_id = _id(listing.item_id)
            payload: dict[str, Any] = {"available_quantity": quantity}
            if listing.variation_id is not None:
                variations = self._variations(self._get_item(item_id))
                if not any(_id(v["id"]) == listing.variation_id for v in variations):
                    raise ValueError("missing target variation")
                # The item API deletes omitted variations: always retain every sibling ID.
                payload = {"variations": [
                    {"id": v["id"], **({"available_quantity": quantity} if _id(v["id"]) == listing.variation_id else {})}
                    for v in variations
                ]}
            result = self.transport.request("PUT", f"/items/{quote(item_id, safe='')}", json=payload)
            if _id(result["id"]) != item_id:
                raise ValueError("updated item identifier")
            self._check_seller(_id(result["seller_id"]))
            if listing.variation_id is not None:
                matches = [v for v in self._variations(result) if _id(v["id"]) == listing.variation_id]
                if len(matches) != 1:
                    raise ValueError("updated variation")
                result = matches[0]
            if _integer(result["available_quantity"]) != quantity:
                raise ValueError("confirmed quantity does not match requested quantity")

    def _get_item(self, item_id: str) -> dict[str, Any]:
        data = self.transport.request("GET", f"/items/{quote(item_id, safe='')}", params={"include_attributes": "all"})
        if _id(data["id"]) != item_id:
            raise ValueError("item identifier")
        self._check_seller(_id(data["seller_id"]))
        return data

    def _check_seller(self, seller_id: str) -> None:
        if seller_id != self.seller_id:
            raise ValueError("seller does not match configured seller")

    @staticmethod
    def _sku(entity: dict[str, Any]) -> str:
        direct = entity.get("seller_custom_field")
        if direct is not None and direct != "":
            return _text(direct) if str(direct).strip() else ""
        values = set()
        attributes = entity.get("attributes")
        for attribute in _array([] if attributes is None else attributes):
            attribute = _object(attribute)
            if attribute.get("id") == "SELLER_SKU" and attribute.get("value_name"):
                values.add(_text(attribute["value_name"]))
        return values.pop() if len(values) == 1 else ""

    @staticmethod
    def _variations(item: dict[str, Any]) -> list[dict[str, Any]]:
        variations = [_object(v) for v in _array(item["variations"])]
        ids = [_id(v["id"]) for v in variations]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate variation identifier")
        return variations

    @staticmethod
    def _page(data: dict[str, Any]) -> tuple[list[Any], int]:
        return _array(data["results"]), _integer(_object(data["paging"])["total"])
