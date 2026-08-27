import json
import os
import requests
import urllib.parse
from meli_client import get_meli_headers, predict_meli_category, get_category_attributes

INPUT_FILE = "shopify_products_full.json"
OUTPUT_FILE = "productos_pendientes.json"

def choose_best_category(product, predictions):
    """Prefer a precise audio/video adapter category for USB-C to Jack/Aux products."""
    title = (product.get("title") or "").lower()
    body = (product.get("body_html") or "").lower()
    product_type = (product.get("product_type") or "").lower()
    searchable_text = " ".join([title, body, product_type])
    audio_terms = ["jack", "3.5", "3,5", "aux", "audio", "audifono", "audífono", "auricular"]

    if any(term in searchable_text for term in audio_terms):
        for prediction in predictions:
            if prediction.get("domain_id") == "MLC-AUDIO_AND_VIDEO_CABLES_AND_ADAPTERS":
                return prediction

    return predictions[0]


def fetch_requirements():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: No se encuentra el archivo {INPUT_FILE}. Ejecuta dump_products primero.")
        return

    with open(INPUT_FILE, "r") as f:
        shopify_data = json.load(f)

    products = shopify_data.get("products", [])
    pending_sync = []

    print(f"Procesando {len(products)} productos para obtener requisitos de Mercado Libre...")

    for idx, p in enumerate(products, start=1):
        title = p.get("title")
        shopify_id = p.get("id")
        print(f"[{idx}/{len(products)}] Analizando: '{title}'...")

        # 1. Predecir categoría
        try:
            predictions = predict_meli_category(title)
            if not predictions:
                print(f"  -> [Ignorado] No se pudo predecir la categoría para '{title}'")
                continue
            best_cat = choose_best_category(p, predictions)
            category_id = best_cat.get("category_id")
            category_name = best_cat.get("category_name")
        except Exception as e:
            print(f"  -> Error al predecir categoría: {e}")
            continue

        # 2. Obtener atributos requeridos
        required_attributes = []
        try:
            attributes = get_category_attributes(category_id)
            # Filtrar solo los obligatorios (excluyendo Marca, Modelo y GTIN que manejamos base)
            required_attributes = [
                {
                    "id": attr.get("id"),
                    "name": attr.get("name"),
                    "value_type": attr.get("value_type"),
                    "values": [v.get("name") for v in attr.get("values", [])[:10]]
                }
                for attr in attributes
                if "required" in attr.get("tags", []) and attr.get("id") not in ["BRAND", "MODEL", "GTIN"]
            ]
        except Exception as e:
            print(f"  -> Advertencia al obtener atributos: {e}")

        # Guardar en la estructura pendiente
        pending_sync.append({
            "shopify_id": shopify_id,
            "original_title": title,
            "vendor": p.get("vendor", "Genérico"),
            "product_type": p.get("product_type", ""),
            "body_html": p.get("body_html", ""),
            "price": p.get("variants", [{}])[0].get("price", 0),
            "stock": p.get("variants", [{}])[0].get("inventory_quantity", 1),
            "barcode": p.get("variants", [{}])[0].get("barcode", ""),
            "images": [img.get("src") for img in p.get("images", [])[:5]],
            "predicted_category": {
                "id": category_id,
                "name": category_name
            },
            "required_attributes_to_fill": required_attributes
        })

    with open(OUTPUT_FILE, "w") as f:
        json.dump(pending_sync, f, indent=4, ensure_ascii=False)
        
    print(f"\n¡Completado! {len(pending_sync)} productos listos para optimización en {OUTPUT_FILE}")

if __name__ == "__main__":
    fetch_requirements()
