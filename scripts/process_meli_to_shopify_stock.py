#!/usr/bin/env python3
"""
Processor for Mercado Libre -> Shopify stock events.

Default mode is dry-run:
- Reads Mercado Libre raw_events or a specific --order-id.
- Fetches /orders/{id}.
- Creates idempotent source=meli stock_tasks per paid order line.
- Resolves Shopify variant by exact SKU and computes target stock.
- Does not update Shopify.

Apply mode:
- Reads ready_to_apply source=meli stock_tasks.
- Re-reads fresh Shopify stock.
- Sets inventory to max(fresh_stock - quantity_sold, 0).
- Confirms Shopify stock and marks the task synced.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DB_FILE = ROOT_DIR / "data" / "stock_sync.db"
TOKENS_FILE = ROOT_DIR / "meli_tokens.json"


class MeliApiError(RuntimeError):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        message = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        super().__init__(f"HTTP {status}: {message}")


class ShopifyApiError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(ROOT_DIR / ".env")

MELI_APP_ID = os.getenv("MELI_APP_ID", "")
MELI_CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET", "")
SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          topic TEXT,
          webhook_id TEXT,
          order_id TEXT,
          order_name TEXT,
          received_at TEXT NOT NULL,
          payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_tasks (
          task_id TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          order_id TEXT NOT NULL,
          order_name TEXT,
          line_item_id TEXT,
          sku TEXT,
          shopify_variant_id TEXT,
          quantity_sold INTEGER NOT NULL,
          status TEXT NOT NULL,
          human_note TEXT,
          line_item_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT,
          event_type TEXT NOT NULL,
          message TEXT NOT NULL,
          data_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_stock_tasks_status
          ON stock_tasks(status);

        CREATE INDEX IF NOT EXISTS idx_stock_tasks_source_status
          ON stock_tasks(source, status);

        CREATE INDEX IF NOT EXISTS idx_stock_tasks_order_id
          ON stock_tasks(order_id);
        """
    )

    add_columns_if_missing(
        conn,
        "raw_events",
        {
            "processed_at": "TEXT",
            "process_status": "TEXT",
            "process_note": "TEXT",
        },
    )
    add_columns_if_missing(
        conn,
        "stock_tasks",
        {
            "shopify_inventory_item_id": "TEXT",
            "shopify_location_id": "TEXT",
            "shopify_stock": "INTEGER",
            "shopify_stock_before": "INTEGER",
            "shopify_target_stock": "INTEGER",
            "meli_item_id": "TEXT",
            "meli_variation_id": "TEXT",
            "meli_match_level": "TEXT",
            "meli_available_quantity": "INTEGER",
            "meli_order_status": "TEXT",
            "meli_sku_source": "TEXT",
            "processed_at": "TEXT",
        },
    )
    conn.commit()


def add_columns_if_missing(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column_name, column_type in columns.items():
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")


def log_event(conn: sqlite3.Connection, task_id: str, event_type: str, message: str, data=None) -> None:
    conn.execute(
        """
        INSERT INTO sync_logs (task_id, event_type, message, data_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, event_type, message, json.dumps(data or {}, ensure_ascii=False), now_iso()),
    )


def update_task(conn: sqlite3.Connection, task_id: str, status: str, note: str, **extra) -> None:
    allowed_extra = {
        "shopify_variant_id",
        "shopify_inventory_item_id",
        "shopify_location_id",
        "shopify_stock",
        "shopify_stock_before",
        "shopify_target_stock",
        "meli_item_id",
        "meli_variation_id",
        "meli_match_level",
        "meli_available_quantity",
        "meli_order_status",
        "meli_sku_source",
        "processed_at",
    }
    set_parts = ["status = ?", "human_note = ?", "updated_at = ?"]
    values = [status, note, now_iso()]

    for key, value in extra.items():
        if key not in allowed_extra:
            raise ValueError(f"Campo no permitido para stock_tasks: {key}")
        set_parts.append(f"{key} = ?")
        values.append(value)

    values.append(task_id)
    conn.execute(
        f"UPDATE stock_tasks SET {', '.join(set_parts)} WHERE task_id = ?",
        values,
    )


def mark_raw_event(conn: sqlite3.Connection, event_id: int, status: str, note: str) -> None:
    conn.execute(
        """
        UPDATE raw_events
        SET processed_at = ?, process_status = ?, process_note = ?
        WHERE id = ?
        """,
        (now_iso(), status, note, event_id),
    )


def require_meli_env() -> None:
    missing = []
    if not MELI_APP_ID:
        missing.append("MELI_APP_ID")
    if not MELI_CLIENT_SECRET:
        missing.append("MELI_CLIENT_SECRET")
    if missing:
        raise RuntimeError(f"Faltan variables en .env: {', '.join(missing)}")


def require_shopify_env() -> None:
    missing = []
    if not SHOPIFY_SHOP_URL:
        missing.append("SHOPIFY_SHOP_URL")
    if not SHOPIFY_ACCESS_TOKEN:
        missing.append("SHOPIFY_ACCESS_TOKEN")
    if missing:
        raise RuntimeError(f"Faltan variables en .env: {', '.join(missing)}")


def request_json(method: str, url: str, headers=None, params=None, json_payload=None, form_payload=None, return_headers=False):
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    body = None
    request_headers = dict(headers or {})
    if json_payload is not None:
        body = json.dumps(json_payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif form_payload is not None:
        body = urllib.parse.urlencode(form_payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            text = resp.read().decode("utf-8")
            payload = json.loads(text) if text else {}
            if return_headers:
                return payload, dict(resp.headers.items())
            return payload
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        raise MeliApiError(exc.code, payload) from exc


def load_meli_tokens() -> dict:
    if not TOKENS_FILE.exists():
        raise RuntimeError("No existe meli_tokens.json. Autentica Mercado Libre primero.")
    return json.loads(TOKENS_FILE.read_text())


def save_meli_tokens(tokens: dict) -> None:
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 21600)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=4))


def refresh_meli_tokens(refresh_token: str) -> dict:
    tokens = request_json(
        "POST",
        "https://api.mercadolibre.com/oauth/token",
        headers={"Accept": "application/json"},
        form_payload={
            "grant_type": "refresh_token",
            "client_id": MELI_APP_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    save_meli_tokens(tokens)
    return tokens


def get_meli_access_token() -> str:
    tokens = load_meli_tokens()
    if time.time() > tokens.get("expires_at", 0) - 120:
        tokens = refresh_meli_tokens(tokens.get("refresh_token"))
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("meli_tokens.json no tiene access_token.")
    return access_token


def meli_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_meli_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def meli_get(url: str, params=None):
    return request_json("GET", url, headers=meli_headers(), params=params or {})


def shopify_domain() -> str:
    return SHOPIFY_SHOP_URL.replace("https://", "").replace("http://", "").strip("/")


def shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def shopify_request_json(method: str, path: str, params=None, json_payload=None, return_headers=False):
    url = f"https://{shopify_domain()}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    try:
        return request_json(
            method,
            url,
            headers=shopify_headers(),
            params=params or {},
            json_payload=json_payload,
            return_headers=return_headers,
        )
    except MeliApiError as exc:
        payload = exc.payload if isinstance(exc.payload, str) else json.dumps(exc.payload, ensure_ascii=False)
        raise ShopifyApiError(f"HTTP {exc.status}: {payload}") from exc


def shopify_get(path: str, params=None):
    return shopify_request_json("GET", path, params=params or {})


def shopify_get_with_headers(path: str, params=None):
    return shopify_request_json("GET", path, params=params or {}, return_headers=True)


def shopify_post(path: str, payload: dict):
    return shopify_request_json("POST", path, json_payload=payload)


def extract_next_link(headers: dict) -> str:
    link_header = headers.get("Link") or headers.get("link") or ""
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if match:
            return match.group(1)
    return ""


def shopify_get_url(url: str):
    try:
        return request_json("GET", url, headers=shopify_headers(), return_headers=True)
    except MeliApiError as exc:
        payload = exc.payload if isinstance(exc.payload, str) else json.dumps(exc.payload, ensure_ascii=False)
        raise ShopifyApiError(f"HTTP {exc.status}: {payload}") from exc


def get_shopify_location_id() -> int:
    data = shopify_get("locations.json")
    for location in data.get("locations", []):
        if location.get("active", True):
            return int(location["id"])
    raise ShopifyApiError("No hay locations activas en Shopify.")


def list_shopify_variants() -> list[dict]:
    params = {"limit": 250, "fields": "id,title,variants"}
    data, headers = shopify_get_with_headers("products.json", params)
    variants = extract_variants_from_products(data)

    next_url = extract_next_link(headers)
    while next_url:
        data, headers = shopify_get_url(next_url)
        variants.extend(extract_variants_from_products(data))
        next_url = extract_next_link(headers)

    return variants


def extract_variants_from_products(data: dict) -> list[dict]:
    variants = []
    for product in data.get("products", []):
        for variant in product.get("variants", []):
            variant_copy = dict(variant)
            variant_copy["product_id"] = product.get("id")
            variant_copy["product_title"] = product.get("title")
            variants.append(variant_copy)
    return variants


def find_shopify_variants_by_sku(sku: str, variants: list[dict] | None = None) -> list[dict]:
    catalog = variants if variants is not None else list_shopify_variants()
    return [
        variant
        for variant in catalog
        if (variant.get("sku") or "").strip() == sku
    ]


def get_shopify_stock_for_inventory_item(inventory_item_id: str | int, location_id: str | int) -> int:
    data = shopify_get(
        "inventory_levels.json",
        {
            "inventory_item_ids": inventory_item_id,
            "location_ids": location_id,
            "limit": 250,
        },
    )
    levels = data.get("inventory_levels", [])
    if not levels:
        raise ShopifyApiError(f"No se encontro inventory_level para inventory_item_id {inventory_item_id}.")
    return int(levels[0].get("available", 0))


def set_shopify_inventory_level(location_id: str | int, inventory_item_id: str | int, quantity: int) -> dict:
    return shopify_post(
        "inventory_levels/set.json",
        {
            "location_id": int(location_id),
            "inventory_item_id": int(inventory_item_id),
            "available": int(quantity),
        },
    )


def get_sku_from_entity(entity: dict) -> str:
    for key in ("seller_custom_field", "seller_sku", "sku"):
        value = (entity.get(key) or "").strip()
        if value:
            return value

    for attribute in entity.get("attributes") or []:
        if attribute.get("id") == "SELLER_SKU":
            return (attribute.get("value_name") or "").strip()

    return ""


def fetch_meli_item(item_id: str) -> dict:
    return meli_get(
        f"https://api.mercadolibre.com/items/{item_id}",
        {
            "attributes": (
                "id,title,seller_custom_field,available_quantity,"
                "attributes,variations"
            )
        },
    )


def resolve_order_item_sku(order_item: dict) -> tuple[str, str]:
    item = order_item.get("item") or {}
    direct_sku = get_sku_from_entity(item)
    if direct_sku:
        return direct_sku, "order_item"

    item_id = item.get("id")
    if not item_id:
        return "", "missing_item_id"

    fetched_item = fetch_meli_item(str(item_id))
    variation_id = item.get("variation_id")
    if variation_id:
        variation_id = str(variation_id)
        for variation in fetched_item.get("variations") or []:
            if str(variation.get("id")) == variation_id:
                variation_sku = get_sku_from_entity(variation)
                return variation_sku, "meli_variation" if variation_sku else "missing_variation_sku"
        return "", "variation_not_found"

    item_sku = get_sku_from_entity(fetched_item)
    return item_sku, "meli_item" if item_sku else "missing_item_sku"


def extract_order_id_from_payload(payload: dict) -> str:
    resource = payload.get("resource") or payload.get("_resource") or ""
    if resource:
        return str(resource).split("/").pop()
    order_id = payload.get("order_id") or payload.get("id")
    return str(order_id) if order_id else ""


def load_meli_raw_events(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM raw_events
        WHERE source = 'meli'
          AND processed_at IS NULL
        ORDER BY received_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def build_meli_task_id(order_id: str, line_index: int, item_id, variation_id) -> str:
    item_part = str(item_id or "no-item")
    variation_part = str(variation_id or "no-variation")
    return f"meli:{order_id}:{line_index}:{item_part}:{variation_part}"


def insert_meli_stock_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    order_id: str,
    order_status: str,
    line_index: int,
    order_item: dict,
    sku: str,
    sku_source: str,
    status: str,
    note: str | None,
) -> bool:
    item = order_item.get("item") or {}
    now = now_iso()
    result = conn.execute(
        """
        INSERT OR IGNORE INTO stock_tasks (
          task_id,
          source,
          order_id,
          order_name,
          line_item_id,
          sku,
          shopify_variant_id,
          quantity_sold,
          status,
          human_note,
          line_item_json,
          created_at,
          updated_at,
          meli_item_id,
          meli_variation_id,
          meli_match_level,
          meli_order_status,
          meli_sku_source
        ) VALUES (?, 'meli', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            order_id,
            f"Meli #{order_id}",
            str(line_index),
            sku or None,
            int(order_item.get("quantity") or 0),
            status,
            note,
            json.dumps(order_item, ensure_ascii=False),
            now,
            now,
            item.get("id"),
            str(item.get("variation_id")) if item.get("variation_id") else None,
            "variation" if item.get("variation_id") else "item",
            order_status,
            sku_source,
        ),
    )
    return result.rowcount > 0


def create_tasks_from_order(conn: sqlite3.Connection, order: dict, event_id=None) -> tuple[int, int]:
    order_id = str(order.get("id") or "")
    task_scope = f"meli:{order_id or 'missing-order-id'}"
    status = order.get("status") or ""
    seller_id = (order.get("seller") or {}).get("id")
    order_items = order.get("order_items") or []

    print(f"  Estado: {status or '(sin estado)'} | Seller: {seller_id or '(sin seller)'}")
    print(f"  Line items: {len(order_items)}")

    if not order_id:
        note = "Orden Meli sin id. No se crean tareas de stock."
        log_event(conn, task_scope, "needs_review", note, {"event_id": event_id, "order": order})
        print(f"  -> needs_review: {note}")
        return 0, 0

    if status != "paid":
        note = f"Orden Meli en estado {status or '(sin estado)'}. No se crean tareas aplicables."
        log_event(conn, task_scope, "skipped_non_paid", note, {"event_id": event_id, "order_id": order_id})
        print(f"  -> skipped_non_paid: {note}")
        return 0, 0

    created = 0
    duplicate = 0
    for index, order_item in enumerate(order_items, start=1):
        item = order_item.get("item") or {}
        sku, sku_source = resolve_order_item_sku(order_item)
        task_id = build_meli_task_id(order_id, index, item.get("id"), item.get("variation_id"))
        quantity = int(order_item.get("quantity") or 0)
        title = item.get("title") or "(sin titulo)"
        sku_text = sku or "(sin SKU)"

        if not sku:
            task_status = "needs_review"
            note = f"Linea Meli sin SKU resoluble: {sku_source}."
        elif quantity <= 0:
            task_status = "needs_review"
            note = f"Linea Meli con cantidad invalida: {quantity}."
        else:
            task_status = "pending"
            note = None

        was_created = insert_meli_stock_task(
            conn,
            task_id=task_id,
            order_id=order_id,
            order_status=status,
            line_index=index,
            order_item=order_item,
            sku=sku,
            sku_source=sku_source,
            status=task_status,
            note=note,
        )

        if was_created:
            created += 1
            log_event(
                conn,
                task_id,
                task_status,
                note or "Tarea Meli creada para dry-run Shopify.",
                {
                    "event_id": event_id,
                    "order_id": order_id,
                    "item_id": item.get("id"),
                    "variation_id": item.get("variation_id"),
                    "sku": sku,
                    "sku_source": sku_source,
                    "quantity": quantity,
                },
            )
        else:
            duplicate += 1
            log_event(conn, task_id, "duplicate_task_ignored", "Tarea Meli ya existia; no se duplico.", {"event_id": event_id})

        print(
            f"  [{index}] {sku_text} x {quantity} | "
            f"{item.get('id') or '(sin item)'}"
            f"{('/' + str(item.get('variation_id'))) if item.get('variation_id') else ''} | "
            f"{title} | sku_source={sku_source} | task={task_status}{' (existia)' if not was_created else ''}"
        )

    print(f"  -> tareas creadas: {created}, existentes: {duplicate}")
    return created, duplicate


def process_order(conn: sqlite3.Connection, order_id: str, event_id=None) -> bool:
    task_id = f"meli:{order_id}"
    print(f"\nProcesando orden Meli {order_id}")
    try:
        order = meli_get(f"https://api.mercadolibre.com/orders/{order_id}")
    except MeliApiError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {"message": str(exc.payload)}
        code = payload.get("code") or payload.get("error")
        if exc.status == 403 and code == "PA_UNAUTHORIZED_RESULT_FROM_POLICIES":
            note = (
                "Meli bloqueo la lectura de ordenes. Revisa en DevCenter el permiso "
                "funcional 'Ventas y envios' para orders/shipments y reautoriza el token."
            )
            log_event(conn, task_id, "meli_orders_permission_blocked", note, payload)
            print(f"  -> bloqueado por permisos: {note}")
            return False

        event_type = "needs_review" if exc.status == 404 else "retryable_error"
        note = f"Error leyendo orden Meli: {exc}"
        log_event(conn, task_id, event_type, note, payload)
        print(f"  -> {event_type}: {note}")
        return exc.status == 404

    create_tasks_from_order(conn, order, event_id=event_id)
    return True


def load_pending_meli_tasks(conn: sqlite3.Connection, limit: int, order_id: str | None = None) -> list[sqlite3.Row]:
    params: list[object] = []
    order_filter = ""
    if order_id:
        order_filter = "AND order_id = ?"
        params.append(order_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT *
        FROM stock_tasks
        WHERE source = 'meli'
          AND status = 'pending'
          {order_filter}
        ORDER BY created_at ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def load_ready_meli_tasks(conn: sqlite3.Connection, limit: int, order_id: str | None = None) -> list[sqlite3.Row]:
    params: list[object] = []
    order_filter = ""
    if order_id:
        order_filter = "AND order_id = ?"
        params.append(order_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT *
        FROM stock_tasks
        WHERE source = 'meli'
          AND status = 'ready_to_apply'
          {order_filter}
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def summarize_shopify_match(match: dict) -> dict:
    return {
        "product_id": match.get("product_id"),
        "product_title": match.get("product_title"),
        "variant_id": match.get("id"),
        "variant_title": match.get("title"),
        "sku": match.get("sku"),
        "inventory_item_id": match.get("inventory_item_id"),
        "inventory_management": match.get("inventory_management"),
    }


def summarize_shopify_matches(matches: list[dict]) -> list[dict]:
    return [summarize_shopify_match(match) for match in matches]


def process_meli_task_dry_run(conn: sqlite3.Connection, task: sqlite3.Row, location_id: int, variants: list[dict]) -> None:
    task_id = task["task_id"]
    sku = (task["sku"] or "").strip()
    quantity = int(task["quantity_sold"] or 0)

    print(f"\nDry-run Shopify para {sku or '(sin SKU)'} ({task['order_name']})")

    if not sku or quantity <= 0:
        note = "Tarea Meli no tiene SKU o cantidad valida. No se puede procesar automaticamente."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note)
        print(f"  -> needs_review: {note}")
        return

    matches = find_shopify_variants_by_sku(sku, variants)
    if len(matches) == 0:
        note = "SKU vendido en Meli no existe en Shopify. No se modifica stock."
        update_task(conn, task_id, "skipped_not_in_shopify", note, processed_at=now_iso())
        log_event(conn, task_id, "skipped_not_in_shopify", note, {"sku": sku})
        print("  -> skipped_not_in_shopify")
        return

    if len(matches) > 1:
        note = "SKU duplicado en Shopify. Se encontraron multiples variantes con el mismo SKU."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note, {"sku": sku, "matches": summarize_shopify_matches(matches)})
        print("  -> needs_review: SKU duplicado en Shopify")
        return

    match = matches[0]
    inventory_item_id = match.get("inventory_item_id")
    inventory_management = match.get("inventory_management")
    if not inventory_item_id or inventory_management != "shopify":
        note = "Variante Shopify no gestiona inventario con Shopify o no tiene inventory_item_id."
        update_task(
            conn,
            task_id,
            "needs_review",
            note,
            shopify_variant_id=str(match.get("id")) if match.get("id") else None,
            shopify_inventory_item_id=str(inventory_item_id) if inventory_item_id else None,
            processed_at=now_iso(),
        )
        log_event(conn, task_id, "needs_review", note, {"sku": sku, "match": summarize_shopify_match(match)})
        print(f"  -> needs_review: {note}")
        return

    try:
        current_stock = get_shopify_stock_for_inventory_item(inventory_item_id, location_id)
    except Exception as exc:
        note = str(exc)
        update_task(conn, task_id, "retryable_error", note, processed_at=now_iso())
        log_event(conn, task_id, "retryable_error", note)
        print(f"  -> retryable_error: {note}")
        return

    target_stock = max(current_stock - quantity, 0)
    note = f"DRY-RUN: Shopify pasaria de stock {current_stock} a {target_stock} para SKU {sku}."
    if quantity > current_stock:
        note += f" Cantidad vendida ({quantity}) excede stock actual; target limitado a 0."

    update_task(
        conn,
        task_id,
        "ready_to_apply",
        note,
        shopify_variant_id=str(match.get("id")),
        shopify_inventory_item_id=str(inventory_item_id),
        shopify_location_id=str(location_id),
        shopify_stock=current_stock,
        shopify_stock_before=current_stock,
        shopify_target_stock=target_stock,
        processed_at=now_iso(),
    )
    log_event(
        conn,
        task_id,
        "ready_to_apply",
        note,
        {
            "sku": sku,
            "quantity_sold": quantity,
            "shopify_variant": summarize_shopify_match(match),
            "location_id": location_id,
            "shopify_stock_before": current_stock,
            "shopify_target_stock": target_stock,
            "clamped_to_zero": quantity > current_stock,
        },
    )

    print(f"  Shopify variant: {match.get('id')} | product: {match.get('product_title')}")
    print(f"  Inventory item: {inventory_item_id} | location: {location_id}")
    print(f"  Stock actual: {current_stock}")
    print(f"  Vendido en Meli: {quantity}")
    print(f"  -> ready_to_apply: Shopify quedaria en {target_stock}")


def apply_meli_task(conn: sqlite3.Connection, task: sqlite3.Row, location_id: int, variants: list[dict]) -> None:
    task_id = task["task_id"]
    sku = (task["sku"] or "").strip()
    quantity = int(task["quantity_sold"] or 0)

    print(f"\nAplicando Shopify para {sku} ({task['order_name']})")

    if not sku or quantity <= 0:
        note = "Tarea ready_to_apply sin SKU o cantidad valida. Requiere revision humana."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note)
        print(f"  -> needs_review: {note}")
        return

    try:
        matches = find_shopify_variants_by_sku(sku, variants)
        if len(matches) != 1:
            note = f"SKU Shopify dejo de ser un match unico antes de aplicar. Matches={len(matches)}."
            update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
            log_event(conn, task_id, "needs_review", note, {"matches": summarize_shopify_matches(matches)})
            print(f"  -> needs_review: {note}")
            return

        match = matches[0]
        inventory_item_id = match.get("inventory_item_id")
        inventory_management = match.get("inventory_management")
        if not inventory_item_id or inventory_management != "shopify":
            note = "Variante Shopify no gestiona inventario con Shopify o no tiene inventory_item_id."
            update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
            log_event(conn, task_id, "needs_review", note, {"match": summarize_shopify_match(match)})
            print(f"  -> needs_review: {note}")
            return

        stock_before = get_shopify_stock_for_inventory_item(inventory_item_id, location_id)
        target_stock = max(stock_before - quantity, 0)
        set_shopify_inventory_level(location_id, inventory_item_id, target_stock)
        confirmed_stock = get_shopify_stock_for_inventory_item(inventory_item_id, location_id)
    except Exception as exc:
        note = str(exc)
        update_task(conn, task_id, "retryable_error", note, processed_at=now_iso())
        log_event(conn, task_id, "retryable_error", note)
        print(f"  -> retryable_error: {note}")
        return

    if confirmed_stock != target_stock:
        note = f"Shopify respondio, pero stock confirmado fue {confirmed_stock}, esperado {target_stock}."
        update_task(
            conn,
            task_id,
            "needs_review",
            note,
            shopify_variant_id=str(match.get("id")),
            shopify_inventory_item_id=str(inventory_item_id),
            shopify_location_id=str(location_id),
            shopify_stock=confirmed_stock,
            shopify_stock_before=stock_before,
            shopify_target_stock=target_stock,
            processed_at=now_iso(),
        )
        log_event(
            conn,
            task_id,
            "needs_review",
            note,
            {
                "sku": sku,
                "quantity_sold": quantity,
                "shopify_stock_before": stock_before,
                "shopify_target_stock": target_stock,
                "shopify_confirmed_stock": confirmed_stock,
            },
        )
        print(f"  -> needs_review: {note}")
        return

    note = f"SYNCED: Shopify SKU {sku} paso de stock {stock_before} a {confirmed_stock} por venta Meli."
    update_task(
        conn,
        task_id,
        "synced",
        note,
        shopify_variant_id=str(match.get("id")),
        shopify_inventory_item_id=str(inventory_item_id),
        shopify_location_id=str(location_id),
        shopify_stock=confirmed_stock,
        shopify_stock_before=stock_before,
        shopify_target_stock=target_stock,
        processed_at=now_iso(),
    )
    log_event(
        conn,
        task_id,
        "synced",
        note,
        {
            "meli_order_id": task["order_id"],
            "sku": sku,
            "quantity_sold": quantity,
            "shopify_variant_id": match.get("id"),
            "shopify_inventory_item_id": inventory_item_id,
            "location_id": location_id,
            "shopify_stock_before": stock_before,
            "shopify_stock_after": confirmed_stock,
        },
    )
    print(f"  Shopify variant: {match.get('id')}")
    print(f"  Stock anterior fresco: {stock_before}")
    print(f"  Stock aplicado/confirmado: {confirmed_stock}")
    print("  -> synced")


def run_permission_check() -> int:
    user_id = None
    try:
        me = meli_get("https://api.mercadolibre.com/users/me")
        user_id = me.get("id")
        print("users/me: OK")
    except MeliApiError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {"message": str(exc.payload)}
        code = payload.get("code") or payload.get("error") or "(sin code)"
        print(f"users/me: HTTP {exc.status} {code}")
        return 2

    checks = [
        ("orders/search", "https://api.mercadolibre.com/orders/search", {"seller": user_id, "limit": 1}),
        ("orders/search/recent", "https://api.mercadolibre.com/orders/search/recent", {"limit": 1}),
    ]
    for name, url, params in checks:
        try:
            meli_get(url, params)
            print(f"{name}: OK")
        except MeliApiError as exc:
            payload = exc.payload if isinstance(exc.payload, dict) else {"message": str(exc.payload)}
            code = payload.get("code") or payload.get("error") or "(sin code)"
            print(f"{name}: HTTP {exc.status} {code}")
    return 0


def process_pending_meli_tasks(conn: sqlite3.Connection, limit: int, order_id: str | None = None) -> None:
    tasks = load_pending_meli_tasks(conn, limit, order_id=order_id)
    if not tasks:
        print("No hay tareas Meli pending para procesar en dry-run.")
        return

    print("\nModo: dry-run Meli -> Shopify (no modifica Shopify)")
    print(f"Tareas Meli pending encontradas: {len(tasks)}")
    location_id = get_shopify_location_id()
    variants = list_shopify_variants()
    print(f"Shopify location activa: {location_id}")
    print(f"Variantes Shopify cargadas para busqueda SKU: {len(variants)}")

    for task in tasks:
        process_meli_task_dry_run(conn, task, location_id, variants)
        conn.commit()

    print("\nDry-run terminado.")


def apply_ready_meli_tasks(conn: sqlite3.Connection, limit: int, order_id: str | None = None) -> None:
    tasks = load_ready_meli_tasks(conn, limit, order_id=order_id)
    if not tasks:
        print("No hay tareas Meli ready_to_apply para aplicar.")
        return

    print("Modo: apply Meli -> Shopify (actualiza Shopify)")
    print(f"Tareas Meli ready_to_apply encontradas: {len(tasks)}")
    location_id = get_shopify_location_id()
    variants = list_shopify_variants()
    print(f"Shopify location activa: {location_id}")

    for task in tasks:
        apply_meli_task(conn, task, location_id, variants)
        conn.commit()

    print("\nApply terminado.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Procesa eventos Mercado Libre -> Shopify.")
    parser.add_argument("--limit", type=int, default=10, help="Maximo de eventos/tareas a procesar.")
    parser.add_argument("--order-id", help="Procesa una orden Meli puntual.")
    parser.add_argument("--check-permissions", action="store_true", help="Prueba permisos basicos de Meli.")
    parser.add_argument("--apply", action="store_true", help="Actualiza Shopify para tareas ready_to_apply.")
    args = parser.parse_args()

    require_meli_env()

    if args.check_permissions:
        return run_permission_check()

    require_shopify_env()

    if not DB_FILE.exists():
        print(f"No existe la base SQLite: {DB_FILE}")
        return 1

    conn = connect_db()

    if args.apply:
        apply_ready_meli_tasks(conn, args.limit, order_id=args.order_id)
        return 0

    if args.order_id:
        ok = process_order(conn, args.order_id)
        conn.commit()
        process_pending_meli_tasks(conn, args.limit, order_id=args.order_id)
        return 0 if ok else 2

    events = load_meli_raw_events(conn, args.limit)
    if not events:
        print("No hay raw_events source=meli sin procesar.")
    else:
        print("Procesando raw_events Meli")
        print(f"Eventos Meli encontrados: {len(events)}")

    any_blocked = False
    for event in events:
        payload = json.loads(event["payload_json"])
        order_id = event["order_id"] or extract_order_id_from_payload(payload)
        if not order_id:
            note = "Evento Meli sin order_id/resource."
            print(f"\nEvento raw {event['id']} no trae order_id/resource. Saltando.")
            log_event(conn, f"meli_raw:{event['id']}", "needs_review", note, payload)
            mark_raw_event(conn, event["id"], "needs_review", note)
            conn.commit()
            continue
        ok = process_order(conn, order_id, event_id=event["id"])
        any_blocked = any_blocked or not ok
        mark_raw_event(conn, event["id"], "processed" if ok else "retryable_error", "Orden procesada" if ok else "Orden no procesada")
        conn.commit()

    process_pending_meli_tasks(conn, args.limit)
    return 2 if any_blocked else 0


if __name__ == "__main__":
    sys.exit(main())
