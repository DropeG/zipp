#!/usr/bin/env python3
"""
Dry-run processor for Shopify -> Mercado Libre stock tasks.

V1 scope:
- Reads pending stock_tasks from data/stock_sync.db.
- Re-reads Shopify to get the current stock for each sold variant/SKU.
- Searches Mercado Libre by exact SKU in item.seller_custom_field.
- Does not update Mercado Libre.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[3]
DB_FILE = ROOT_DIR / "data" / "stock_sync.db"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file(ROOT_DIR / ".env")

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

MELI_APP_ID = os.getenv("MELI_APP_ID", "")
MELI_CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET", "")
TOKENS_FILE = ROOT_DIR / "meli_tokens.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sync_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT,
          event_type TEXT NOT NULL,
          message TEXT NOT NULL,
          data_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sku_cache (
          sku TEXT PRIMARY KEY,
          meli_item_id TEXT,
          meli_variation_id TEXT,
          match_level TEXT,
          verified_at TEXT,
          status TEXT,
          last_error TEXT
        );
        """
    )

    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(stock_tasks)").fetchall()
    }
    columns_to_add = {
        "shopify_stock": "INTEGER",
        "meli_item_id": "TEXT",
        "meli_variation_id": "TEXT",
        "meli_match_level": "TEXT",
        "meli_available_quantity": "INTEGER",
        "processed_at": "TEXT",
    }
    for column_name, column_type in columns_to_add.items():
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE stock_tasks ADD COLUMN {column_name} {column_type}")
    conn.commit()


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
        "shopify_stock",
        "meli_item_id",
        "meli_variation_id",
        "meli_match_level",
        "meli_available_quantity",
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


def require_env() -> None:
    missing = []
    if not SHOPIFY_SHOP_URL:
        missing.append("SHOPIFY_SHOP_URL")
    if not SHOPIFY_ACCESS_TOKEN:
        missing.append("SHOPIFY_ACCESS_TOKEN")
    if not MELI_APP_ID:
        missing.append("MELI_APP_ID")
    if not MELI_CLIENT_SECRET:
        missing.append("MELI_CLIENT_SECRET")
    if missing:
        raise RuntimeError(f"Faltan variables en .env: {', '.join(missing)}")


def shopify_domain() -> str:
    return SHOPIFY_SHOP_URL.replace("https://", "").replace("http://", "").strip("/")


def shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def request_json(method: str, url: str, headers=None, params=None, json_payload=None, form_payload=None):
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
            if not text:
                return {}
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {error_text}") from exc


def shopify_get(path: str, params=None) -> dict:
    url = f"https://{shopify_domain()}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    try:
        return request_json("GET", url, headers=shopify_headers(), params=params or {})
    except RuntimeError as exc:
        raise RuntimeError(f"Shopify API error: {exc}") from exc


def get_shopify_location_id() -> int:
    data = shopify_get("locations.json")
    for location in data.get("locations", []):
        if location.get("active", True):
            return int(location["id"])
    raise RuntimeError("No hay locations activas en Shopify.")


def get_shopify_stock_for_variant(variant_id: str, expected_sku: str, location_id: int) -> int:
    variant_data = shopify_get(f"variants/{variant_id}.json")
    variant = variant_data.get("variant") or {}
    actual_sku = (variant.get("sku") or "").strip()
    if actual_sku != expected_sku:
        raise RuntimeError(
            f"SKU Shopify no coincide. Task={expected_sku}, variant={actual_sku or '(sin SKU)'}"
        )

    inventory_item_id = variant.get("inventory_item_id")
    if not inventory_item_id:
        raise RuntimeError(f"Variant {variant_id} no tiene inventory_item_id.")

    levels_data = shopify_get(
        "inventory_levels.json",
        {
            "inventory_item_ids": inventory_item_id,
            "location_ids": location_id,
            "limit": 250,
        },
    )
    levels = levels_data.get("inventory_levels", [])
    if not levels:
        raise RuntimeError(f"No se encontro inventory_level para SKU {expected_sku}.")
    return int(levels[0].get("available", 0))


def load_meli_tokens() -> dict:
    if not TOKENS_FILE.exists():
        raise RuntimeError("No existe meli_tokens.json. Autentica Mercado Libre primero.")
    return json.loads(TOKENS_FILE.read_text())


def save_meli_tokens(tokens: dict) -> None:
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 21600)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=4))


def refresh_meli_tokens(refresh_token: str) -> dict:
    try:
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
    except RuntimeError as exc:
        raise RuntimeError(f"Error refrescando token Meli: {exc}") from exc
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
    try:
        return request_json("GET", url, headers=meli_headers(), params=params or {})
    except RuntimeError as exc:
        raise RuntimeError(f"Meli API error: {exc}") from exc


def meli_put(url: str, payload: dict):
    try:
        return request_json("PUT", url, headers=meli_headers(), json_payload=payload)
    except RuntimeError as exc:
        raise RuntimeError(f"Meli API error: {exc}") from exc


def get_meli_user_id() -> int:
    data = meli_get("https://api.mercadolibre.com/users/me")
    return int(data["id"])


def list_meli_item_ids(user_id: int, limit_total: int = 1000) -> list[str]:
    item_ids = []
    offset = 0
    page_size = 50
    while len(item_ids) < limit_total:
        data = meli_get(
            f"https://api.mercadolibre.com/users/{user_id}/items/search",
            {"limit": page_size, "offset": offset},
        )
        results = data.get("results", [])
        if not results:
            break
        item_ids.extend(results)
        if len(results) < page_size:
            break
        offset += page_size
    return item_ids[:limit_total]


def fetch_meli_items(item_ids: list[str]) -> list[dict]:
    items = []
    for index in range(0, len(item_ids), 20):
        chunk = item_ids[index:index + 20]
        data = meli_get(
            "https://api.mercadolibre.com/items",
            {
                "ids": ",".join(chunk),
                "attributes": "id,title,status,seller_custom_field,available_quantity,attributes,variations",
            },
        )
        for wrapper in data:
            if wrapper.get("code") == 200 and wrapper.get("body"):
                items.append(wrapper["body"])
    return items


def get_meli_entity_sku(entity: dict) -> str:
    direct_sku = (entity.get("seller_custom_field") or "").strip()
    if direct_sku:
        return direct_sku

    for attribute in entity.get("attributes") or []:
        if attribute.get("id") == "SELLER_SKU":
            return (attribute.get("value_name") or "").strip()

    return ""


def find_meli_matches_by_sku(sku: str, item_ids: list[str]) -> list[dict]:
    matches = []
    for item in fetch_meli_items(item_ids):
        item_sku = get_meli_entity_sku(item)
        if item_sku == sku:
            matches.append(
                {
                    "match_level": "item",
                    "meli_item_id": item.get("id"),
                    "meli_variation_id": None,
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "available_quantity": item.get("available_quantity"),
                }
            )

        for variation in item.get("variations") or []:
            variation_sku = get_meli_entity_sku(variation)
            if variation_sku == sku:
                matches.append(
                    {
                        "match_level": "variation",
                        "meli_item_id": item.get("id"),
                        "meli_variation_id": variation.get("id"),
                        "title": item.get("title"),
                        "status": item.get("status"),
                        "available_quantity": variation.get("available_quantity"),
                    }
                )
    return matches


def load_pending_tasks(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM stock_tasks
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def load_ready_to_apply_tasks(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM stock_tasks
        WHERE status = 'ready_to_apply'
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def process_task(conn: sqlite3.Connection, task, location_id: int, meli_item_ids: list[str]) -> None:
    task_id = task["task_id"]
    sku = task["sku"]
    variant_id = task["shopify_variant_id"]

    print(f"\nProcesando {sku} ({task['order_name']})")

    if not sku or not variant_id:
        note = "Tarea no tiene SKU o variant_id. No se puede procesar automaticamente."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note)
        print(f"  -> needs_review: {note}")
        return

    try:
        shopify_stock = get_shopify_stock_for_variant(str(variant_id), sku, location_id)
        matches = find_meli_matches_by_sku(sku, meli_item_ids)
    except Exception as exc:
        note = str(exc)
        update_task(conn, task_id, "retryable_error", note, processed_at=now_iso())
        log_event(conn, task_id, "retryable_error", note)
        print(f"  -> retryable_error: {note}")
        return

    if len(matches) == 0:
        note = "SKU existe en Shopify pero no se encontro en Mercado Libre. Probablemente aun no esta publicado/sincronizado."
        update_task(
            conn,
            task_id,
            "skipped_not_in_meli",
            note,
            shopify_stock=shopify_stock,
            processed_at=now_iso(),
        )
        log_event(conn, task_id, "skipped_not_in_meli", note, {"shopify_stock": shopify_stock})
        print(f"  Shopify stock actual: {shopify_stock}")
        print("  -> skipped_not_in_meli")
        return

    if len(matches) > 1:
        note = "SKU duplicado en Mercado Libre. Se encontraron multiples publicaciones/variaciones con el mismo SKU."
        update_task(
            conn,
            task_id,
            "needs_review",
            note,
            shopify_stock=shopify_stock,
            processed_at=now_iso(),
        )
        log_event(conn, task_id, "needs_review", note, {"shopify_stock": shopify_stock, "matches": matches})
        print(f"  Shopify stock actual: {shopify_stock}")
        print("  -> needs_review: SKU duplicado en Meli")
        return

    match = matches[0]
    if match["match_level"] != "item":
        note = "SKU encontrado en una variacion de Meli. V1 dry-run lo detecta, pero aun no actualiza variaciones automaticamente."
        update_task(
            conn,
            task_id,
            "needs_review",
            note,
            shopify_stock=shopify_stock,
            meli_item_id=match["meli_item_id"],
            meli_variation_id=str(match["meli_variation_id"]),
            meli_match_level=match["match_level"],
            meli_available_quantity=match["available_quantity"],
            processed_at=now_iso(),
        )
        log_event(conn, task_id, "needs_review", note, {"shopify_stock": shopify_stock, "match": match})
        print(f"  Shopify stock actual: {shopify_stock}")
        print("  -> needs_review: match en variacion")
        return

    note = (
        f"DRY-RUN: Meli {match['meli_item_id']} pasaria de stock "
        f"{match['available_quantity']} a {shopify_stock}."
    )
    update_task(
        conn,
        task_id,
        "ready_to_apply",
        note,
        shopify_stock=shopify_stock,
        meli_item_id=match["meli_item_id"],
        meli_variation_id=None,
        meli_match_level=match["match_level"],
        meli_available_quantity=match["available_quantity"],
        processed_at=now_iso(),
    )
    log_event(conn, task_id, "ready_to_apply", note, {"shopify_stock": shopify_stock, "match": match})
    print(f"  Shopify stock actual: {shopify_stock}")
    print(f"  Meli item: {match['meli_item_id']} ({match['title']})")
    print(f"  Meli stock actual: {match['available_quantity']}")
    print(f"  -> ready_to_apply: Meli quedaria en {shopify_stock}")


def get_meli_item_stock(item_id: str) -> int:
    item = meli_get(
        f"https://api.mercadolibre.com/items/{item_id}",
        {"attributes": "id,available_quantity"},
    )
    return int(item.get("available_quantity", 0))


def update_meli_item_stock(item_id: str, stock: int) -> None:
    meli_put(
        f"https://api.mercadolibre.com/items/{item_id}",
        {"available_quantity": int(stock)},
    )


def apply_task(conn: sqlite3.Connection, task) -> None:
    task_id = task["task_id"]
    sku = task["sku"]
    item_id = task["meli_item_id"]
    shopify_stock = task["shopify_stock"]
    match_level = task["meli_match_level"]
    meli_before = task["meli_available_quantity"]

    print(f"\nAplicando {sku} ({task['order_name']})")

    if not item_id or shopify_stock is None:
        note = "Tarea ready_to_apply sin meli_item_id o shopify_stock. Requiere revision humana."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note)
        print(f"  -> needs_review: {note}")
        return

    if match_level != "item":
        note = "V1 --apply solo actualiza publicaciones simples de Meli, no variaciones."
        update_task(conn, task_id, "needs_review", note, processed_at=now_iso())
        log_event(conn, task_id, "needs_review", note)
        print(f"  -> needs_review: {note}")
        return

    try:
        update_meli_item_stock(item_id, int(shopify_stock))
        confirmed_stock = get_meli_item_stock(item_id)
    except Exception as exc:
        note = str(exc)
        update_task(conn, task_id, "retryable_error", note, processed_at=now_iso())
        log_event(conn, task_id, "retryable_error", note)
        print(f"  -> retryable_error: {note}")
        return

    if confirmed_stock != int(shopify_stock):
        note = f"Meli respondio OK, pero el stock confirmado fue {confirmed_stock}, esperado {shopify_stock}."
        update_task(
            conn,
            task_id,
            "needs_review",
            note,
            meli_available_quantity=confirmed_stock,
            processed_at=now_iso(),
        )
        log_event(
            conn,
            task_id,
            "needs_review",
            note,
            {"meli_before": meli_before, "shopify_stock": shopify_stock, "meli_after": confirmed_stock},
        )
        print(f"  -> needs_review: {note}")
        return

    note = f"SYNCED: Meli {item_id} paso de stock {meli_before} a {confirmed_stock}."
    update_task(
        conn,
        task_id,
        "synced",
        note,
        meli_available_quantity=confirmed_stock,
        processed_at=now_iso(),
    )
    log_event(
        conn,
        task_id,
        "synced",
        note,
        {"meli_before": meli_before, "shopify_stock": shopify_stock, "meli_after": confirmed_stock},
    )
    print(f"  Meli item: {item_id}")
    print(f"  Stock anterior visto en dry-run: {meli_before}")
    print(f"  Stock aplicado/confirmado: {confirmed_stock}")
    print("  -> synced")


def main() -> int:
    parser = argparse.ArgumentParser(description="Procesa tareas Shopify -> Meli.")
    parser.add_argument("--limit", type=int, default=10, help="Maximo de tareas a procesar.")
    parser.add_argument("--apply", action="store_true", help="Actualiza Mercado Libre para tareas ready_to_apply.")
    args = parser.parse_args()

    require_env()

    if not DB_FILE.exists():
        print(f"No existe la base SQLite: {DB_FILE}")
        return 1

    conn = connect_db()

    if args.apply:
        tasks = load_ready_to_apply_tasks(conn, args.limit)
        if not tasks:
            print("No hay tareas ready_to_apply para aplicar.")
            return 0

        print("Modo: apply (actualiza Mercado Libre)")
        print(f"Tareas ready_to_apply encontradas: {len(tasks)}")

        for task in tasks:
            apply_task(conn, task)
            conn.commit()

        print("\nApply terminado.")
        return 0

    tasks = load_pending_tasks(conn, args.limit)
    if not tasks:
        print("No hay tareas pending para procesar.")
        return 0

    print("Modo: dry-run (no actualiza Mercado Libre)")
    print(f"Tareas pending encontradas: {len(tasks)}")

    location_id = get_shopify_location_id()
    print(f"Shopify location activa: {location_id}")

    user_id = get_meli_user_id()
    item_ids = list_meli_item_ids(user_id)
    print(f"Publicaciones Meli cargadas para busqueda SKU: {len(item_ids)}")

    for task in tasks:
        process_task(conn, task, location_id, item_ids)
        conn.commit()

    print("\nDry-run terminado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
