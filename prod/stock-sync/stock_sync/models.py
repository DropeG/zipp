from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    source_key: str
    resource_key: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: str
    last_error: str | None


@dataclass(frozen=True)
class MeliOrderLine:
    item_id: str
    variation_id: str | None
    title: str
    quantity: int
    unit_price: str
    currency: str
    sku: str


@dataclass(frozen=True)
class MeliOrder:
    order_id: str
    seller_id: str
    status: str
    processed_at: str
    lines: list[MeliOrderLine]


@dataclass(frozen=True)
class ShopifyVariant:
    variant_id: str
    sku: str
    available_quantity: int
    inventory_tracked: bool


@dataclass(frozen=True)
class MeliListing:
    item_id: str
    variation_id: str | None
    sku: str
    available_quantity: int
