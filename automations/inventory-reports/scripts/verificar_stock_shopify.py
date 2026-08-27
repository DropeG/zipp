"""
verificar_stock_shopify.py
==========================
Consulta el stock REAL en Shopify para cada SKU del Excel
y lo compara con el valor 'Disponible' del Excel.
Genera un reporte de verificación.
"""

import os
import time
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

SHOPIFY_SHOP_URL     = os.getenv("SHOPIFY_SHOP_URL", "")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION  = os.getenv("SHOPIFY_API_VERSION", "2024-04")
EXCEL_COL_SKU        = "Código"
EXCEL_COL_STOCK      = "Disponible"
REQUESTS_DELAY       = 0.3


def _shop_domain():
    return SHOPIFY_SHOP_URL.replace("https://", "").replace("http://", "").strip("/")

def _headers():
    return {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}


def get_location_id():
    url  = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/locations.json"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    for loc in resp.json().get("locations", []):
        if loc.get("active", True):
            return loc["id"]
    raise RuntimeError("No hay locations activas.")


def build_variant_map():
    """Devuelve {sku: inventory_item_id}"""
    sku_map = {}
    url     = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    params  = {"limit": 250, "fields": "id,variants"}
    while url:
        resp = requests.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        for product in resp.json().get("products", []):
            for v in product.get("variants", []):
                sku = (v.get("sku") or "").strip()
                if sku:
                    sku_map[sku] = v["inventory_item_id"]
        next_link = resp.links.get("next")
        url    = next_link["url"] if next_link else None
        params = {}
        time.sleep(REQUESTS_DELAY)
    return sku_map


def get_inventory_levels_batch(inventory_item_ids, location_id):
    """
    Consulta los niveles de inventario para una lista de inventory_item_ids.
    Devuelve {inventory_item_id: quantity_available}
    """
    levels = {}
    # La API acepta hasta 50 IDs por llamada
    chunk_size = 50
    for i in range(0, len(inventory_item_ids), chunk_size):
        chunk = inventory_item_ids[i:i+chunk_size]
        ids_str = ",".join(str(x) for x in chunk)
        url  = f"https://{_shop_domain()}/admin/api/{SHOPIFY_API_VERSION}/inventory_levels.json"
        params = {
            "inventory_item_ids": ids_str,
            "location_ids": location_id,
            "limit": 250,
        }
        resp = requests.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        for level in resp.json().get("inventory_levels", []):
            levels[level["inventory_item_id"]] = level["available"]
        time.sleep(REQUESTS_DELAY)
    return levels


def run(excel_path):
    print(f"\n{'='*65}")
    print(f"  Verificación de Stock — Shopify vs Excel")
    print(f"{'='*65}\n")

    # 1. Leer Excel — solo los 83 que estaban en Shopify
    print("📂 Leyendo Excel...")
    df = pd.read_excel(excel_path, sheet_name=0)
    df[EXCEL_COL_SKU]   = df[EXCEL_COL_SKU].astype(str).str.strip()
    df[EXCEL_COL_STOCK] = pd.to_numeric(df[EXCEL_COL_STOCK], errors="coerce").fillna(0).astype(int)
    df = df[df[EXCEL_COL_SKU].notna() & (df[EXCEL_COL_SKU] != "") & (df[EXCEL_COL_SKU] != "nan")]
    # Eliminar duplicados de SKU (quedarse con el primero)
    df_unico = df.drop_duplicates(subset=[EXCEL_COL_SKU], keep="first")

    # 2. Obtener catálogo Shopify
    print("🔍 Obteniendo catálogo de Shopify...")
    sku_map    = build_variant_map()
    location_id = get_location_id()
    print(f"   → {len(sku_map)} variantes encontradas. Location ID: {location_id}")

    # 3. Filtrar solo SKUs que existen en Shopify
    skus_en_shopify = df_unico[df_unico[EXCEL_COL_SKU].isin(sku_map.keys())]
    print(f"   → {len(skus_en_shopify)} SKUs del Excel presentes en Shopify.")

    # 4. Obtener stock real de Shopify en batch
    print("\n📡 Consultando stock real en Shopify...")
    inv_item_ids = [sku_map[sku] for sku in skus_en_shopify[EXCEL_COL_SKU]]
    niveles      = get_inventory_levels_batch(inv_item_ids, location_id)

    # 5. Comparar
    print(f"\n{'─'*65}")
    print(f"{'SKU':<35} {'Excel':>7} {'Shopify':>9} {'Estado':>12}")
    print(f"{'─'*65}")

    ok = []
    diferencias = []
    sin_nivel   = []

    for _, row in skus_en_shopify.iterrows():
        sku         = row[EXCEL_COL_SKU]
        stock_excel = int(row[EXCEL_COL_STOCK])
        inv_id      = sku_map[sku]
        stock_real  = niveles.get(inv_id)

        if stock_real is None:
            sin_nivel.append({"SKU": sku, "Stock Excel": stock_excel, "Stock Shopify": "—"})
            print(f"  ❓  {sku:<33} {stock_excel:>7} {'—':>9}  Sin nivel registrado")
        elif stock_real == stock_excel:
            ok.append({"SKU": sku, "Stock Excel": stock_excel, "Stock Shopify": stock_real})
            print(f"  ✅  {sku:<33} {stock_excel:>7} {stock_real:>9}  Coincide")
        else:
            diferencias.append({"SKU": sku, "Stock Excel": stock_excel, "Stock Shopify": stock_real,
                                 "Diferencia": stock_real - stock_excel})
            print(f"  ❌  {sku:<33} {stock_excel:>7} {stock_real:>9}  DIFERENCIA: {stock_real - stock_excel:+d}")

    # 6. Resumen
    print(f"\n{'='*65}")
    print(f"  RESULTADO DE VERIFICACIÓN")
    print(f"{'='*65}")
    print(f"  ✅ Coinciden perfectamente : {len(ok)}")
    print(f"  ❌ Con diferencia          : {len(diferencias)}")
    print(f"  ❓ Sin nivel en Shopify    : {len(sin_nivel)}")

    if diferencias:
        print(f"\n  ⚠️  Productos con diferencia:")
        print(f"  {'SKU':<35} {'Excel':>7} {'Shopify':>9} {'Diff':>6}")
        for d in diferencias:
            print(f"  {d['SKU']:<35} {d['Stock Excel']:>7} {d['Stock Shopify']:>9} {d['Diferencia']:>+6}")

    if not diferencias and not sin_nivel:
        print(f"\n  🎉 ¡Verificación 100% exitosa! Todo el stock coincide.")
    elif not diferencias:
        print(f"\n  ⚠️  Stock coincide en todos los encontrados, pero {len(sin_nivel)} no tienen nivel registrado.")

    print(f"{'='*65}\n")
    return ok, diferencias, sin_nivel


if __name__ == "__main__":
    import sys
    excel = sys.argv[1] if len(sys.argv) > 1 else "/Users/pedro/Downloads/Stock Zipp Jun 26 2026.xlsx"
    run(excel)
