import os
import json
import requests
from shopify_client import get_shopify_products
from meli_client import get_meli_headers, predict_meli_category
from ai_client import optimize_product_with_ai

def validate_item_on_meli(meli_item):
    """Envía la publicación al endpoint de validación de Mercado Libre."""
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/items/validate"
    response = requests.post(url, headers=headers, json=meli_item)
    return response

def build_meli_payload(optimized_data, category_id, stock, price, pictures, use_catalog_format=False):
    """Construye la carga útil para Mercado Libre dependiendo del formato."""
    payload = {
        "category_id": category_id,
        "price": int(price),
        "currency_id": "CLP",
        "available_quantity": stock,
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": "bronze",
        "shipping": {
            "mode": "me2",
            "local_pick_up": True,
            "free_shipping": True
        },
        "pictures": pictures
    }

    if use_catalog_format:
        # Formato de Catálogo (User Products) - No lleva 'title'
        payload["family_name"] = optimized_data["optimized_title"]
        payload["attributes"] = [
            {
                "id": "BRAND",
                "value_name": optimized_data["brand"]
            },
            {
                "id": "MODEL",
                "value_name": optimized_data["model"]
            }
        ]
    else:
        # Formato Estándar - Lleva 'title' y opcionalmente atributos sencillos
        payload["title"] = optimized_data["optimized_title"]
        payload["attributes"] = [
            {
                "id": "BRAND",
                "value_name": optimized_data["brand"]
            },
            {
                "id": "MODEL",
                "value_name": optimized_data["model"]
            }
        ]

    return payload

def run_ai_snychronization_test():
    print("=== PASO 1: OBTENIENDO PRODUCTO DE SHOPIFY ===")
    shopify_data = get_shopify_products(limit=1)
    products = shopify_data.get("products", [])
    if not products:
        print("No hay productos en Shopify.")
        return
    product = products[0]
    print(f"Producto real obtenido: '{product.get('title')}'")

    print("\n=== PASO 2: OPTIMIZANDO CON GEMINI 2.5-FLASH ===")
    # Extraer variables
    title = product.get("title")
    product_type = product.get("product_type", "")
    vendor = product.get("vendor", "")
    body_html = product.get("body_html", "")
    
    # Llamada a Gemini
    ai_result = optimize_product_with_ai(title, product_type, vendor, body_html)
    print("\n[IA] Datos Optimizados por Gemini:")
    print(json.dumps(ai_result, indent=4, ensure_ascii=False))

    print("\n=== PASO 3: PREDICTOR INTELIGENTE DE CATEGORÍA DE MERCADO LIBRE ===")
    # Obtener el título optimizado por IA y predecir su categoría
    optimized_title = ai_result.get("optimized_title")
    predictions = predict_meli_category(optimized_title)
    
    if not predictions:
        print("No se pudieron obtener predicciones de categoría.")
        return
        
    # Seleccionar la mejor predicción (la primera en la lista)
    best_prediction = predictions[0]
    category_id = best_prediction.get("category_id")
    category_name = best_prediction.get("category_name")
    print(f"\n[Predictor] Categoría recomendada:")
    print(f"- ID: {category_id}")
    print(f"- Nombre: {category_name}")

    # Preparar precio, stock e imágenes
    variants = product.get("variants", [])
    price = 0
    stock = 1
    if variants:
        price = float(variants[0].get("price", 0))
        stock = variants[0].get("inventory_quantity", 1)
        if stock <= 0:
            stock = 1
            
    images = product.get("images", [])
    meli_pictures = [{"source": img.get("src")} for img in images[:5]]

    print("\n=== PASO 4: VALIDACIÓN DINÁMICA ADAPTATIVA ===")
    
    # Intentamos primero con el formato Estándar (que usa 'title')
    print("Probando formato de publicación Estándar...")
    meli_item = build_meli_payload(ai_result, category_id, stock, price, meli_pictures, use_catalog_format=False)
    response = validate_item_on_meli(meli_item)
    
    is_valid = False
    if response.status_code in [200, 204]:
        print("\n¡ÉXITO! El formato Estándar es compatible.")
        is_valid = True
    elif response.status_code == 400:
        res_json = response.json()
        causes = res_json.get("cause", [])
        
        # Analizar causas de error
        errors = [c for c in causes if c.get("type") == "error"]
        warnings = [c for c in causes if c.get("type") == "warning"]
        
        # ¿El error es porque requiere formato de catálogo (family_name)?
        requires_catalog = False
        for err in errors:
            if "family_name" in err.get("message", "") or "family_name" in err.get("references", []):
                requires_catalog = True
            if "title" in err.get("message", "") and "invalid" in err.get("message", ""):
                requires_catalog = True
                
        # Si el error indicaba que 'title' es inválido o falta 'family_name', nos adaptamos
        if res_json.get("message") == "body.invalid_fields" and "title" in res_json.get("error", ""):
            requires_catalog = True

        if requires_catalog:
            print("\n[Auto-Corrección] Mercado Libre requiere formato de Catálogo (User Products) para esta categoría.")
            print("Adaptando payload y re-intentando validación...")
            
            # Re-construimos en formato catálogo (usa family_name, no lleva title)
            meli_item_cat = build_meli_payload(ai_result, category_id, stock, price, meli_pictures, use_catalog_format=True)
            response_cat = validate_item_on_meli(meli_item_cat)
            
            if response_cat.status_code in [200, 204]:
                print("¡ÉXITO! El formato de Catálogo adaptado es compatible.")
                is_valid = True
            elif response_cat.status_code == 400:
                res_cat_json = response_cat.json()
                cat_causes = res_cat_json.get("cause", [])
                cat_errors = [c for c in cat_causes if c.get("type") == "error"]
                cat_warnings = [c for c in cat_causes if c.get("type") == "warning"]
                
                if cat_warnings and not cat_errors:
                    print("¡ÉXITO! El formato de Catálogo adaptado es compatible (con advertencias de envío).")
                    is_valid = True
                else:
                    print(f"Error de validación en Catálogo: {response_cat.status_code}")
                    print(response_cat.text)
            else:
                print(f"Error inesperado en Catálogo: {response_cat.status_code}")
                print(response_cat.text)
        else:
            # Si solo eran warnings del formato estándar
            if warnings and not errors:
                print("¡ÉXITO! El formato Estándar es compatible (con advertencias de envío).")
                is_valid = True
            else:
                print(f"Error de validación en formato Estándar: {response.status_code}")
                print(response.text)
    else:
        print(f"Error inesperado en primer intento: {response.status_code}")
        print(response.text)

    if is_valid:
        print("\n==============================================")
        print("¡VALIDACIÓN DEL PASO 3 COMPLETADA CON ÉXITO!")
        print("La IA y el Predictor de Categorías funcionan.")
        print("==============================================")

if __name__ == "__main__":
    try:
        run_ai_snychronization_test()
    except Exception as e:
        print(f"\nError durante la ejecución del integrador de IA: {e}")
