import os
import json
import argparse
import time
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.shopify_client import get_shopify_products
from shared.meli_client import (
    get_meli_headers, 
    predict_meli_category, 
    get_category_attributes,
    publish_meli_item,
    check_meli_item_exists,
    update_meli_item_price_and_stock
)
import requests

MAPPINGS_FILE = AUTOMATION_DIR / "sync_mappings.json"

def load_mappings():
    """Carga el registro de mapeos locales entre Shopify y Mercado Libre."""
    if os.path.exists(MAPPINGS_FILE):
        try:
            with open(MAPPINGS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error al cargar sync_mappings.json: {e}. Creando uno nuevo...")
    return {}

def save_mappings(mappings):
    """Guarda el registro de mapeos locales en el archivo JSON."""
    try:
        with open(MAPPINGS_FILE, "w") as f:
            json.dump(mappings, f, indent=4)
        print("Mapeos de sincronización actualizados localmente.")
    except Exception as e:
        print(f"Error al guardar sync_mappings.json: {e}")

def validate_item_on_meli(meli_item):
    """Valida la publicación de prueba."""
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/items/validate"
    response = requests.post(url, headers=headers, json=meli_item)
    return response

def build_meli_payload(optimized_data, category_id, stock, price, pictures, extra_attributes=None, barcode=None, use_catalog_format=False, shipping=None, variations=None):
    """Construye la carga útil adaptada para Mercado Libre."""
    shipping_config = shipping or {}
    if not shipping_config:
        searchable_text = " ".join([
            optimized_data.get("optimized_title", ""),
            optimized_data.get("clean_description", ""),
            optimized_data.get("model", "")
        ]).lower()
        restricted_shipping_terms = [
            "bateria",
            "batería",
            "battery",
            "power bank",
            "magsafe",
            "mah",
            "litio",
            "lithium"
        ]
        if any(term in searchable_text for term in restricted_shipping_terms):
            shipping_config = {
                "mode": "not_specified",
                "local_pick_up": True,
                "free_shipping": True
            }
        else:
            shipping_config = {
                "mode": "me2",
                "local_pick_up": True,
                "free_shipping": True
            }

    payload = {
        "category_id": category_id,
        "price": int(price),
        "currency_id": "CLP",
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": "bronze", # Clásica
        "shipping": shipping_config,
        "pictures": pictures
    }
    if variations:
        payload["variations"] = build_meli_variations(variations, price)
    else:
        payload["available_quantity"] = stock

    # Atributos base obligatorios
    base_attributes = [
        {
            "id": "BRAND",
            "value_name": optimized_data.get("brand", "Genérico")
        },
        {
            "id": "MODEL",
            "value_name": optimized_data.get("model", "Accesorio")
        }
    ]

    # Mercado Libre requires numeric GTIN values when the GTIN attribute is
    # sent. For products without a registered barcode, declare the empty-code
    # reason instead of sending placeholder text such as "No aplica".
    gtin_value = str(barcode).strip() if barcode and str(barcode).strip() else ""
    empty_gtin_values = {"no aplica", "n/a", "na", "sin gtin", "sin codigo", "sin código"}
    if gtin_value and gtin_value.lower() not in empty_gtin_values:
        base_attributes.append({
            "id": "GTIN",
            "value_name": gtin_value
        })
    else:
        base_attributes.append({
            "id": "EMPTY_GTIN_REASON",
            "value_id": "17055160"
        })

    # Incorporar atributos extra extraídos por la IA (si existen)
    variation_attribute_ids = {
        combo.get("id")
        for variation in payload.get("variations", [])
        for combo in variation.get("attribute_combinations", [])
        if combo.get("id")
    }
    base_attributes = [
        attr for attr in base_attributes
        if attr.get("id") not in variation_attribute_ids
    ]

    existing_attribute_ids = {attr["id"] for attr in base_attributes}
    if extra_attributes:
        for attr in extra_attributes:
            # Asegurarse de no duplicar
            if attr.get("id") not in existing_attribute_ids and attr.get("id") not in variation_attribute_ids:
                base_attributes.append(attr)
                existing_attribute_ids.add(attr.get("id"))

    payload["attributes"] = base_attributes

    if use_catalog_format:
        # Formato de Catálogo (User Products)
        payload["family_name"] = optimized_data["optimized_title"]
    else:
        # Formato Estándar
        payload["title"] = optimized_data["optimized_title"]

    return payload

def build_meli_variations(variations, fallback_price):
    """Convert agent-prepared Shopify variants into Mercado Libre variations."""
    meli_variations = []
    for variation in variations:
        attribute_combinations = variation.get("attribute_combinations") or []
        if not attribute_combinations:
            continue

        stock = variation.get("available_quantity", variation.get("stock", 0))
        price = variation.get("price", fallback_price)
        meli_variation = {
            "attribute_combinations": attribute_combinations,
            "available_quantity": int(stock or 0),
            "price": int(float(price or fallback_price or 0))
        }

        seller_custom_field = variation.get("seller_custom_field") or variation.get("sku") or variation.get("shopify_variant_id")
        if seller_custom_field:
            meli_variation["seller_custom_field"] = str(seller_custom_field)

        picture_ids = variation.get("picture_ids") or variation.get("images") or []
        if picture_ids:
            meli_variation["picture_ids"] = picture_ids

        attributes = variation.get("attributes") or []
        if attributes:
            meli_variation["attributes"] = attributes

        meli_variations.append(meli_variation)

    return meli_variations

def sync_all_products(limit=5, dry_run=True):
    from shared.ai_client import optimize_product_with_ai, extract_attributes_with_ai

    print("==========================================================")
    print(f"INICIANDO INTEGRACIÓN DE PRODUCTOS (Modo Prueba: {dry_run})")
    print("==========================================================")
    
    # 1. Cargar mapeos locales para control de duplicados
    mappings = load_mappings()
    print(f"Cargados {len(mappings)} productos ya sincronizados anteriormente.")

    # 2. Leer productos de Shopify
    print("\n[Shopify] Obteniendo productos activos...")
    shopify_data = get_shopify_products(limit=limit)
    products = shopify_data.get("products", [])
    
    if not products:
        print("No se encontraron productos en Shopify.")
        return

    print(f"Se procesarán hasta {len(products)} productos.")

    for idx, product in enumerate(products, start=1):
        shopify_id = str(product.get("id"))
        title = product.get("title")
        
        print(f"\n----------------------------------------------------------")
        print(f"Procesando [{idx}/{len(products)}]: '{title}' (ID: {shopify_id})")
        print(f"----------------------------------------------------------")

        # 1. Extraer variantes de Shopify (precio, stock y código de barras)
        variants = product.get("variants", [])
        if not variants:
            print("  [ERROR] El producto de Shopify no tiene variantes. Saltando...")
            continue
        variant = variants[0]
        price = float(variant.get("price", 0.0))
        stock = int(variant.get("inventory_quantity", 0))
        if stock <= 0:
            stock = 1  # Evitar errores de validación de stock cero
        barcode = variant.get("barcode", "")

        # 2. Control de imágenes vacías
        images = product.get("images", [])
        if not images:
            print("  [Saltado] El producto no tiene imágenes en Shopify (requerido por Mercado Libre).")
            continue

        # 3. Control de duplicados y actualización de stock/precio
        if shopify_id in mappings:
            meli_id = mappings[shopify_id]
            if check_meli_item_exists(meli_id):
                if dry_run:
                    print(f"  [MODO PRUEBA] Producto ya sincronizado. Se actualizaría precio a ${price} y stock a {stock} en {meli_id}")
                else:
                    update_meli_item_price_and_stock(meli_id, price, stock)
                continue
            else:
                print(f"[Alerta] La publicación {meli_id} ya no existe en Mercado Libre. Remapeando...")
                del mappings[shopify_id]
                save_mappings(mappings)

        # Extraer imágenes
        meli_pictures = [{"source": img.get("src")} for img in images[:5]]

        # Paso A: Optimizar con IA
        try:
            ai_data = optimize_product_with_ai(
                title=title,
                product_type=product.get("product_type", ""),
                vendor=product.get("vendor", ""),
                body_html=product.get("body_html", "")
            )
            print(f"  -> Título Optimizado: '{ai_data['optimized_title']}'")
            print(f"  -> Marca: {ai_data['brand']} | Modelo: {ai_data['model']}")
        except Exception as e:
            print(f"  [ERROR] Falló optimización de IA para '{title}': {e}")
            continue

        # Paso B: Predecir Categoría
        try:
            predictions = predict_meli_category(ai_data["optimized_title"])
            if not predictions:
                print("  [ERROR] No se pudo predecir la categoría.")
                continue
            category_id = predictions[0].get("category_id")
            category_name = predictions[0].get("category_name")
            print(f"  -> Categoría sugerida: {category_name} ({category_id})")
        except Exception as e:
            print(f"  [ERROR] Falló predicción de categoría: {e}")
            continue

        # Paso C: Consultar Atributos requeridos para esa categoría
        extra_attributes = []
        try:
            attributes = get_category_attributes(category_id)
            # Buscar atributos requeridos por la categoría (excluyendo marca y modelo)
            required_attributes = [
                attr for attr in attributes 
                if "required" in attr.get("tags", []) and attr.get("id") not in ["BRAND", "MODEL"]
            ]
            
            if required_attributes:
                print(f"  -> Categoría requiere {len(required_attributes)} especificaciones técnicas.")
                # Pedir a Gemini que extraiga estos atributos del producto
                extra_attributes = extract_attributes_with_ai(
                    title=title,
                    description=ai_data["clean_description"],
                    required_attributes=required_attributes
                )
                print(f"  -> Atributos extraídos: {extra_attributes}")
        except Exception as e:
            print(f"  [ADVERTENCIA] No se pudieron analizar los atributos requeridos: {e}")

        # Paso D: Construir y Validar de forma adaptativa
        use_catalog = False
        print("  -> Validando publicación en Mercado Libre...")
        meli_item = build_meli_payload(ai_data, category_id, stock, price, meli_pictures, extra_attributes, barcode=barcode, use_catalog_format=False)
        response = validate_item_on_meli(meli_item)
        
        is_valid = False
        if response.status_code in [200, 204]:
            is_valid = True
        elif response.status_code == 400:
            res_json = response.json()
            causes = res_json.get("cause", [])
            errors = [c for c in causes if c.get("type") == "error"]
            warnings = [c for c in causes if c.get("type") == "warning"]

            # Si nos indica que requiere el formato de catálogo (family_name) o da error por 'title'
            requires_catalog = False
            for err in errors:
                if "family_name" in err.get("message", "") or "family_name" in err.get("references", []):
                    requires_catalog = True
                if "title" in err.get("message", "") and "invalid" in err.get("message", ""):
                    requires_catalog = True
            if res_json.get("message") == "body.invalid_fields" and "title" in res_json.get("error", ""):
                requires_catalog = True

            if requires_catalog:
                print("  [Auto-Corrección] Re-intentando en formato de catálogo (User Products)...")
                use_catalog = True
                meli_item = build_meli_payload(ai_data, category_id, stock, price, meli_pictures, extra_attributes, barcode=barcode, use_catalog_format=True)
                response = validate_item_on_meli(meli_item)
                
                if response.status_code in [200, 204]:
                    is_valid = True
                elif response.status_code == 400:
                    res_json = response.json()
                    errors = [c for c in res_json.get("cause", []) if c.get("type") == "error"]
                    warnings = [c for c in res_json.get("cause", []) if c.get("type") == "warning"]
                    if warnings and not errors:
                        is_valid = True

            elif warnings and not errors:
                is_valid = True

        if not is_valid:
            print(f"  [ERROR] Falló la validación final en Mercado Libre (HTTP {response.status_code}):")
            print(f"  {response.text}")
            continue

        # Paso E: Publicar (o reportar éxito en modo prueba)
        if dry_run:
            print(f"  [MODO PRUEBA] ¡Producto listo para publicar! Formato: {'Catálogo' if use_catalog else 'Estándar'}")
            # Simulamos el ID guardando un ID temporal en modo prueba para que el usuario vea el log
            # Pero NO guardamos en los mapeos persistentes para poder publicarlo de verdad después
        else:
            print(f"  -> Publicando en Mercado Libre de forma real...")
            try:
                result = publish_meli_item(meli_item)
                meli_item_id = result.get("id")
                print(f"  [PUBLICADO] ¡Sincronizado con éxito! Mercado Libre ID: {meli_item_id}")
                
                # Subir descripción en texto plano
                update_meli_item_description(meli_item_id, ai_data["clean_description"])
                
                # Guardar mapeo para evitar duplicidad
                mappings[shopify_id] = meli_item_id
                save_mappings(mappings)
            except Exception as e:
                print(f"  [ERROR] Falló la publicación final en Mercado Libre: {e}")

        # Retraso para evitar límites de tasa (rate limiting) en Shopify y Mercado Libre
        time.sleep(0.5)

    print("\n==========================================================")
    print("PROCESO DE SINCRONIZACIÓN FINALIZADO")
    print("==========================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agente de sincronización inteligente de Shopify a Mercado Libre.")
    parser.add_argument("--limit", type=int, default=5, help="Límite de productos a procesar (por defecto 5).")
    parser.add_argument("--publish", action="store_true", help="Publica de forma real los productos (si no se especifica, corre en modo prueba/validación).")
    
    args = parser.parse_args()
    
    # Si --publish está marcado, dry_run = False
    dry_run = not args.publish
    
    run_ai_snychronization_test = sync_all_products(limit=args.limit, dry_run=dry_run)
