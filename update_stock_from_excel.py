"""
update_stock_from_excel.py
==========================
Actualiza el stock de inventario en Shopify usando un archivo Excel.

Mapeo de columnas del Excel:
  - 'Código'      → SKU de la variante en Shopify
  - 'Disponible'  → Stock disponible a sincronizar (Stock Físico - Asignado)

Uso:
  # Modo simulación (NO modifica nada en Shopify):
  python update_stock_from_excel.py --excel "/ruta/al/archivo.xlsx"

  # Modo real (modifica el inventario en Shopify):
  python update_stock_from_excel.py --excel "/ruta/al/archivo.xlsx" --apply
"""

import os
import sys
import time
import argparse
import csv
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
#  Configuración
# ──────────────────────────────────────────────
SHOPIFY_SHOP_URL      = os.getenv("SHOPIFY_SHOP_URL", "")
SHOPIFY_ACCESS_TOKEN  = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION   = os.getenv("SHOPIFY_API_VERSION", "2024-04")

EXCEL_COL_SKU   = "Código"
EXCEL_COL_STOCK = "Disponible"

REQUESTS_DELAY = 0.5   # segundos entre llamadas (respetar rate limit de Shopify)


# ──────────────────────────────────────────────
#  Helpers HTTP
# ──────────────────────────────────────────────

def _shop_domain() -> str:
    return SHOPIFY_SHOP_URL.replace("https://", "").replace("http://", "").strip("/")


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def _get(endpoint: str, params: dict = None):
    url = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/{endpoint}"
    resp = requests.get(url, headers=_headers(), params=params or {})
    resp.raise_for_status()
    return resp.json(), resp


def _post(endpoint: str, payload: dict) -> dict:
    url = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/{endpoint}"
    resp = requests.post(url, headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


# ──────────────────────────────────────────────
#  Paso 1: Location ID principal
# ──────────────────────────────────────────────

def get_primary_location_id() -> int:
    data, _ = _get("locations.json")
    locations = data.get("locations", [])
    if not locations:
        raise RuntimeError("No se encontraron locations en la tienda Shopify.")
    for loc in locations:
        if loc.get("active", True):
            print(f"✅ Location: '{loc['name']}' (ID: {loc['id']})")
            return loc["id"]
    raise RuntimeError("No hay locations activas en Shopify.")


# ──────────────────────────────────────────────
#  Paso 2: Mapa SKU → inventory_item_id
# ──────────────────────────────────────────────

def build_sku_map() -> dict:
    """Devuelve {sku: inventory_item_id} para todas las variantes de Shopify."""
    print("\n🔍 Descargando catálogo de Shopify...")
    sku_map = {}
    url = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    params = {"limit": 250, "fields": "id,title,variants"}

    while url:
        resp = requests.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()

        for product in data.get("products", []):
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if sku:
                    sku_map[sku] = variant["inventory_item_id"]

        next_link = resp.links.get("next")
        if next_link:
            url = next_link["url"]
            params = {}
        else:
            url = None

        time.sleep(REQUESTS_DELAY)

    print(f"   → {len(sku_map)} variantes con SKU encontradas en Shopify.")
    return sku_map


# ──────────────────────────────────────────────
#  Paso 3: Actualizar nivel de inventario
# ──────────────────────────────────────────────

def set_inventory_level(location_id: int, inventory_item_id: int, quantity: int) -> dict:
    return _post("inventory_levels/set.json", {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": quantity,
    })


# ──────────────────────────────────────────────
#  Paso 4: Leer Excel
# ──────────────────────────────────────────────

def read_excel(path: str) -> "pd.DataFrame":
    print(f"\n📂 Leyendo Excel: {path}")
    df = pd.read_excel(path, sheet_name=0)

    missing = [c for c in [EXCEL_COL_SKU, EXCEL_COL_STOCK] if c not in df.columns]
    if missing:
        raise ValueError(f"El Excel no tiene las columnas requeridas: {missing}")

    df[EXCEL_COL_SKU]   = df[EXCEL_COL_SKU].astype(str).str.strip()
    df[EXCEL_COL_STOCK] = pd.to_numeric(df[EXCEL_COL_STOCK], errors="coerce").fillna(0).astype(int)

    # Filtrar filas sin SKU válido
    df = df[df[EXCEL_COL_SKU].notna() & (df[EXCEL_COL_SKU] != "") & (df[EXCEL_COL_SKU] != "nan")]
    print(f"   → {len(df)} filas con SKU válido en el Excel.")
    return df


# ──────────────────────────────────────────────
#  Motor principal
# ──────────────────────────────────────────────

def run(excel_path: str, apply: bool):
    if not SHOPIFY_SHOP_URL or not SHOPIFY_ACCESS_TOKEN:
        raise ValueError("Faltan SHOPIFY_SHOP_URL o SHOPIFY_ACCESS_TOKEN en el archivo .env")

    mode_label = "🚀 MODO REAL (modificará Shopify)" if apply else "🧪 DRY-RUN (simulación — sin cambios reales)"
    print(f"\n{'='*60}")
    print(f"  Actualizador de Stock Shopify desde Excel")
    print(f"  {mode_label}")
    print(f"{'='*60}")

    location_id = get_primary_location_id()
    sku_map     = build_sku_map()
    df          = read_excel(excel_path)

    results   = []
    not_found = []
    errors    = []

    print(f"\n{'─'*60}")
    print(f"{'SKU':<35} {'Stock':>8}  Estado")
    print(f"{'─'*60}")

    for _, row in df.iterrows():
        sku   = row[EXCEL_COL_SKU]
        stock = int(row[EXCEL_COL_STOCK])

        if sku not in sku_map:
            not_found.append({"sku": sku, "stock_excel": stock})
            print(f"  ⚠️  {sku:<33} {stock:>8}  No encontrado en Shopify")
            continue

        inventory_item_id = sku_map[sku]

        if apply:
            try:
                set_inventory_level(location_id, inventory_item_id, stock)
                results.append({"sku": sku, "stock_nuevo": stock, "estado": "OK"})
                print(f"  ✅  {sku:<33} {stock:>8}  Actualizado")
                time.sleep(REQUESTS_DELAY)
            except Exception as e:
                errors.append({"sku": sku, "stock_excel": stock, "error": str(e)})
                print(f"  ❌  {sku:<33} {stock:>8}  Error: {e}")
        else:
            results.append({"sku": sku, "stock_nuevo": stock, "estado": "DRY-RUN"})
            print(f"  🔵  {sku:<33} {stock:>8}  Simulado")

    # Resumen
    print(f"\n{'='*60}")
    print(f"  RESUMEN")
    print(f"{'='*60}")
    print(f"  Total filas Excel   : {len(df)}")
    print(f"  ✅ Procesados       : {len(results)}")
    print(f"  ⚠️  No encontrados  : {len(not_found)}")
    print(f"  ❌ Errores          : {len(errors)}")

    # Reporte CSV
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = f"reporte_stock_{timestamp}.csv"

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "stock_nuevo", "estado"])
        writer.writeheader()
        for r in results:
            writer.writerow(r)
        for nf in not_found:
            writer.writerow({"sku": nf["sku"], "stock_nuevo": nf["stock_excel"], "estado": "NO_ENCONTRADO_EN_SHOPIFY"})
        for e in errors:
            writer.writerow({"sku": e["sku"], "stock_nuevo": e["stock_excel"], "estado": f"ERROR: {e['error']}"})

    print(f"\n  📄 Reporte: {report_path}")

    if not apply:
        print("\n  ⚡ Para aplicar los cambios reales, agrega el flag --apply")

    return results, not_found, errors


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Actualiza el stock de Shopify desde un Excel de inventario."
    )
    parser.add_argument(
        "--excel",
        required=True,
        help="Ruta al archivo Excel (ej: 'Stock Zipp Jun 26 2026.xlsx')",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Aplica los cambios en Shopify. Sin este flag, solo simula (dry-run).",
    )
    args = parser.parse_args()
    run(excel_path=args.excel, apply=args.apply)
