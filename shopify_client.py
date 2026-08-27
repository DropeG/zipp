import os
import requests
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

def get_shopify_products(limit=50):
    """
    Obtiene la lista de productos de Shopify usando el token estático.
    """
    if not SHOPIFY_SHOP_URL or not SHOPIFY_ACCESS_TOKEN:
        raise ValueError(
            "Faltan las credenciales de Shopify en las variables de entorno. "
            "Asegúrate de definir SHOPIFY_SHOP_URL y SHOPIFY_ACCESS_TOKEN en tu archivo .env."
        )

    # Limpiar el dominio
    shop_domain = SHOPIFY_SHOP_URL.replace("https://", "").replace("http://", "").strip("/")
    
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    }
    params = {
        "limit": limit
    }
    
    all_products = []
    while url:
        print(f"Conectando a Shopify en: {url}...")
        response = requests.get(url, headers=headers, params=params, timeout=60)
        
        if response.status_code != 200:
            raise Exception(
                f"Error al conectar con Shopify: {response.status_code} - {response.text}"
            )
            
        data = response.json()
        all_products.extend(data.get("products", []))
        
        # Shopify usa paginación por cursor. Si hay otra página, 'response.links' tendrá la clave 'next'
        next_link = response.links.get("next")
        if next_link:
            url = next_link.get("url")
            # Para las páginas siguientes, los parámetros ya vienen en la URL (page_info)
            params = {}
        else:
            url = None
            
    return {"products": all_products}

if __name__ == "__main__":
    try:
        products_data = get_shopify_products(limit=5)
        products = products_data.get("products", [])
        print(f"\n¡Conexión exitosa! Se obtuvieron {len(products)} productos.")
        for product in products:
            print(f"- ID: {product.get('id')} | Título: {product.get('title')} | Tipo: {product.get('product_type')}")
    except Exception as e:
        print(f"\nError al ejecutar la conexión: {e}")
