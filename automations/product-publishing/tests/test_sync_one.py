import os
import requests
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.shopify_client import get_shopify_products
from shared.meli_client import get_meli_headers

def test_sync_first_product():
    print("=== PASO 1: OBTENIENDO PRODUCTO REAL DE SHOPIFY ===")
    shopify_data = get_shopify_products(limit=1)
    products = shopify_data.get("products", [])
    
    if not products:
        print("No se encontraron productos en la tienda de Shopify.")
        return
        
    product = products[0]
    print(f"\nProducto obtenido con éxito de Shopify:")
    print(f"- ID: {product.get('id')}")
    print(f"- Título: {product.get('title')}")
    print(f"- Marca (Vendor): {product.get('vendor')}")
    print(f"- Tipo de Producto: {product.get('product_type')}")
    
    # Extraer la primera variante para precio e inventario
    variants = product.get("variants", [])
    price = 0
    stock = 1
    if variants:
        variant = variants[0]
        price = float(variant.get("price", 0))
        stock = variant.get("inventory_quantity", 1)
        if stock <= 0:
            stock = 1 # Para evitar errores de validación de stock cero
            
    print(f"- Precio Variant: {price} CLP")
    print(f"- Stock Variant: {stock}")

    # Extraer las imágenes del CDN de Shopify
    images = product.get("images", [])
    meli_pictures = []
    for img in images[:5]: # Máximo 5 imágenes para la prueba
        meli_pictures.append({
            "source": img.get("src")
        })
    print(f"- Imágenes encontradas: {len(meli_pictures)}")

    print("\n=== PASO 2: MAPEAR DATOS AL FORMATO DE MERCADO LIBRE ===")
    
    # Mapearemos este producto a la categoría MLC9729 (Hubs USB) como prueba
    # Usaremos el vendor de Shopify como marca y una palabra clave para el modelo
    brand = product.get("vendor", "Genérico")
    model = product.get("product_type", "Accesorio") or "Accesorio"
    
    meli_item = {
        "category_id": "MLC9729",
        "price": int(price),
        "currency_id": "CLP",
        "available_quantity": stock,
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": "bronze",
        "family_name": product.get("title"), # Usamos el título como family_name para User Products (UP)
        "attributes": [
            {
                "id": "BRAND",
                "value_name": brand
            },
            {
                "id": "MODEL",
                "value_name": model
            }
        ],
        "shipping": {
            "mode": "me2",
            "local_pick_up": True,
            "free_shipping": True
        },
        "pictures": meli_pictures
    }

    print("\nPayload generado para Mercado Libre:")
    print(f"- Categoría ID: {meli_item['category_id']}")
    print(f"- Family Name (Título): {meli_item['family_name']}")
    print(f"- Marca: {brand} | Modelo: {model}")
    print(f"- Precio: {meli_item['price']} CLP")
    print(f"- Imágenes: {[p['source'] for p in meli_item['pictures']]}")

    print("\n=== PASO 3: ENVIANDO A VALIDACIÓN DE MERCADO LIBRE ===")
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/items/validate"
    
    response = requests.post(url, headers=headers, json=meli_item)
    
    # Si devuelve 200, 204 o un 400 que solo tenga warnings, se considera válido
    if response.status_code in [200, 204]:
        print("\n¡ÉXITO! El producto real de Shopify es 100% compatible con Mercado Libre y está listo para publicar.")
    elif response.status_code == 400:
        res_json = response.json()
        causes = res_json.get("cause", [])
        
        # Filtrar si solo hay advertencias (warnings)
        errors = [c for c in causes if c.get("type") == "error"]
        warnings = [c for c in causes if c.get("type") == "warning"]
        
        if warnings and not errors:
            print("\n¡ÉXITO! El producto es compatible para publicación en Mercado Libre.")
            print("Advertencias informativas del canal de envíos:")
            for w in warnings:
                print(f"  - [{w.get('code')}]: {w.get('message')}")
        else:
            print(f"\nError de validación (campos incorrectos): {response.status_code}")
            print(response.text)
    else:
        print(f"\nError inesperado en la API de Mercado Libre: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    try:
        test_sync_first_product()
    except Exception as e:
        print(f"\nOcurrió un error al ejecutar la prueba de sincronización: {e}")
