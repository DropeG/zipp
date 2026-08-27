import os
import time
import json
import urllib.parse
import requests
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

MELI_APP_ID = os.getenv("MELI_APP_ID")
MELI_CLIENT_SECRET = os.getenv("MELI_CLIENT_SECRET")
MELI_REDIRECT_URI = os.getenv("MELI_REDIRECT_URI", "https://localhost")
TOKENS_FILE = "meli_tokens.json"

def save_tokens(tokens):
    """Guarda los tokens de Mercado Libre en un archivo JSON local."""
    # Guardamos también la marca de tiempo de cuándo expira el access_token
    # Por lo general dura 21600 segundos (6 horas)
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 21600)
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=4)
    print("Tokens guardados correctamente en meli_tokens.json.")

def load_tokens():
    """Carga los tokens del archivo JSON local si existe y es válido."""
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error al leer los tokens locales: {e}")
    return None

def get_authorization_url():
    """Genera la URL para iniciar la autorización de OAuth con Mercado Libre Chile (MLC)."""
    params = {
        "response_type": "code",
        "client_id": MELI_APP_ID,
        "redirect_uri": MELI_REDIRECT_URI
    }
    # Para Chile usamos el dominio auth.mercadolibre.cl
    url = f"https://auth.mercadolibre.cl/authorization?{urllib.parse.urlencode(params)}"
    return url

def exchange_code_for_tokens(auth_code):
    """Intercambia el código de autorización temporal por los tokens permanentes."""
    url = "https://api.mercadolibre.com/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    data = {
        "grant_type": "authorization_code",
        "client_id": MELI_APP_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "code": auth_code,
        "redirect_uri": MELI_REDIRECT_URI
    }
    
    print("Intercambiando código por tokens...")
    response = requests.post(url, headers=headers, data=data)
    if response.status_code != 200:
        raise Exception(f"Error al intercambiar tokens: {response.status_code} - {response.text}")
        
    tokens = response.json()
    save_tokens(tokens)
    return tokens

def refresh_tokens(refresh_token):
    """Refresca el access_token usando el refresh_token correspondiente."""
    url = "https://api.mercadolibre.com/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    data = {
        "grant_type": "refresh_token",
        "client_id": MELI_APP_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "refresh_token": refresh_token
    }
    
    print("Refrescando token de acceso...")
    response = requests.post(url, headers=headers, data=data)
    if response.status_code != 200:
        raise Exception(f"Error al refrescar tokens: {response.status_code} - {response.text}")
        
    tokens = response.json()
    save_tokens(tokens)
    return tokens

def get_meli_access_token():
    """
    Retorna un access_token válido. 
    Si no existen tokens locales, guía al usuario para la autenticación inicial.
    Si el token está próximo a expirar, lo refresca de forma automática.
    """
    if not MELI_APP_ID or not MELI_CLIENT_SECRET:
        raise ValueError(
            "Faltan MELI_APP_ID y/o MELI_CLIENT_SECRET en las variables de entorno (.env)."
        )

    tokens = load_tokens()

    # 1. Autenticación Inicial
    if not tokens:
        print("\n=== AUTENTICACIÓN INICIAL CON MERCADO LIBRE ===")
        auth_url = get_authorization_url()
        print("1. Abre la siguiente URL en tu navegador e inicia sesión con tu cuenta de Mercado Libre:")
        print(f"\n{auth_url}\n")
        print("2. Tras autorizar, serás redirigido a una URL (por defecto localhost).")
        print("3. Copia el parámetro '?code=TG-...' de esa URL.")
        auth_code = input("\nIntroduce el código de autorización (TG-...): ").strip()
        
        if not auth_code:
            raise ValueError("El código de autorización no puede estar vacío.")
            
        tokens = exchange_code_for_tokens(auth_code)

    # 2. Verificar expiración (dejamos un margen de 2 minutos para evitar fallos a medio camino)
    elif time.time() > tokens.get("expires_at", 0) - 120:
        print("El token de Mercado Libre ha expirado o está cerca de expirar.")
        tokens = refresh_tokens(tokens.get("refresh_token"))

    return tokens.get("access_token")

def get_meli_headers():
    """Genera las cabeceras de autorización necesarias para las llamadas a la API."""
    access_token = get_meli_access_token()
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def get_meli_user_me():
    """Obtiene los datos del usuario autenticado."""
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/users/me"
    print("Obteniendo información del usuario de Mercado Libre...")
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener usuario: {response.status_code} - {response.text}")
    return response.json()

def predict_meli_category(title):
    """
    Predice la categoría de Mercado Libre Chile (MLC) basándose en el título del producto.
    Retorna la predicción de categorías recomendadas utilizando el endpoint moderno de Domain Discovery.
    """
    headers = get_meli_headers()
    escaped_title = urllib.parse.quote(title)
    url = f"https://api.mercadolibre.com/sites/MLC/domain_discovery/search?q={escaped_title}"
    
    print(f"Consultando Domain Discovery de Mercado Libre para: '{title}'...")
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al predecir categoría: {response.status_code} - {response.text}")
        
    return response.json()

def get_category_attributes(category_id):
    """Obtiene los atributos definidos para una categoría específica en Mercado Libre."""
    headers = get_meli_headers()
    url = f"https://api.mercadolibre.com/categories/{category_id}/attributes"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener atributos de la categoría {category_id}: {response.status_code} - {response.text}")
    return response.json()

def publish_meli_item(meli_item):
    """
    Publica un producto real en la API de Mercado Libre.
    Retorna el JSON de respuesta con el ID del producto creado.
    """
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/items"
    response = requests.post(url, headers=headers, json=meli_item)
    if response.status_code not in [200, 201]:
        raise Exception(f"Error al publicar producto real: {response.status_code} - {response.text}")
    return response.json()

def update_meli_item_description(item_id, plain_text):
    """
    Asigna o actualiza la descripción en texto plano de un ítem en Mercado Libre.
    """
    headers = get_meli_headers()
    url = f"https://api.mercadolibre.com/items/{item_id}/description"
    payload = {
        "plain_text": plain_text
    }
    print(f"  -> Subiendo descripción a Mercado Libre para el ítem {item_id}...")
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 400 and "use PUT instead" in response.text:
        response = requests.put(url, headers=headers, json=payload)
    if response.status_code not in [200, 201]:
        print(f"  [ADVERTENCIA] No se pudo subir la descripción al ítem {item_id}: {response.status_code} - {response.text}")
        return False
    return True

def update_meli_item_price_and_stock(item_id, price, stock):
    """
    Actualiza el precio y la cantidad disponible de un ítem existente en Mercado Libre.
    """
    headers = get_meli_headers()
    url = f"https://api.mercadolibre.com/items/{item_id}"
    payload = {
        "price": price,
        "available_quantity": stock
    }
    print(f"  -> Sincronizando precio ({price}) y stock ({stock}) en Mercado Libre para {item_id}...")
    response = requests.put(url, headers=headers, json=payload)
    if response.status_code not in [200, 204]:
        print(f"  [ERROR] No se pudo actualizar precio/stock del ítem {item_id}: {response.status_code} - {response.text}")
        return False
    return True






def validate_test_listing():
    """
    Valida un producto de prueba en la API de Mercado Libre Chile (MLC).
    Utiliza el endpoint /items/validate que no genera cobros ni publicaciones reales.
    """
    headers = get_meli_headers()
    url = "https://api.mercadolibre.com/items/validate"
    
    # Payload mínimo adaptado para probar en Chile (CLP)
    # Usaremos la categoría hoja MLC9729 (Hubs USB)
    test_item = {
        "category_id": "MLC9729",
        "price": 5000,
        "currency_id": "CLP",
        "available_quantity": 10,
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": "bronze", # Publicación clásica
        "family_name": "Hubs USB C de Prueba",
        "attributes": [
            {
                "id": "BRAND",
                "value_name": "Genérico"
            },
            {
                "id": "MODEL",
                "value_name": "HUB-USBC-TEST"
            }
        ],
        "shipping": {
            "mode": "me2",
            "local_pick_up": True,
            "free_shipping": True
        },
        "pictures": [
            {
                "source": "https://upload.wikimedia.org/wikipedia/commons/d/d7/Android_robot.svg"
            }
        ]
    }
    
    print("Validando publicación de prueba en Mercado Libre Chile (MLC)...")
    response = requests.post(url, headers=headers, json=test_item)
    
    if response.status_code in [200, 204]:
        print("¡Publicación de prueba validada correctamente! El esquema de datos es compatible.")
        return True
    else:
        print(f"Error en la validación: {response.status_code} - {response.text}")
        return False

def check_meli_item_exists(meli_item_id):
    """
    Verifica directamente en la API de Mercado Libre si la publicación existe y sigue activa.
    """
    headers = get_meli_headers()
    url = f"https://api.mercadolibre.com/items/{meli_item_id}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            return False
        if response.status_code == 200:
            item = response.json()
            status = item.get("status")
            sub_status = item.get("sub_status") or []
            if status in ["deleted"] or "deleted" in sub_status:
                return False
            return True
        # Si devuelve 403 o similar, asumimos que existe pero hay restricción de permisos
        if response.status_code == 403:
            return True
    except Exception as e:
        print(f"Error al verificar item {meli_item_id} en Mercado Libre: {e}")
        return True
    return True

if __name__ == "__main__":
    try:
        user_info = get_meli_user_me()
        print(f"\n¡Conexión a Mercado Libre exitosa!")
        print(f"- Usuario ID: {user_info.get('id')}")
        print(f"- Apodo (Nickname): {user_info.get('nickname')}")
        print(f"- País: {user_info.get('site_id')}")
        
        print("\nProcediendo con la validación de publicación de prueba...")
        validate_test_listing()
    except Exception as e:
        print(f"\nError en el flujo de Mercado Libre: {e}")
