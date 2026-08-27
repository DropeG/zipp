import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv

try:
    from shared.repo_paths import REPO_ROOT
except ModuleNotFoundError:
    from repo_paths import REPO_ROOT

# Cargar variables de entorno desde el archivo .env
load_dotenv(REPO_ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("ADVERTENCIA: GEMINI_API_KEY no encontrada en las variables de entorno.")

def optimize_product_with_ai(title, product_type, vendor, body_html):
    """
    Usa la API de Gemini 2.5-flash para optimizar el título para SEO en Mercado Libre,
    extraer los atributos obligatorios (Marca y Modelo), y limpiar la descripción.
    """
    if not GEMINI_API_KEY:
        raise ValueError(
            "Falta la variable GEMINI_API_KEY en tu archivo .env. "
            "Asegúrate de conseguir una clave de API de Gemini y definirla."
        )

    # Inicializar el cliente oficial GenAI
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
Eres un experto en SEO para Mercado Libre Chile (MLC). Tu tarea es optimizar los datos de un producto de Shopify para que sea perfecto para su publicación en Mercado Libre.

Datos del producto en Shopify:
- Título original: {title}
- Tipo de producto: {product_type}
- Proveedor/Marca original: {vendor}
- Descripción original (puede tener HTML): {body_html}

Tus objetivos son:
1. **Optimizar el Título**: Crea un título optimizado para el buscador de Mercado Libre Chile.
   - Debe tener un máximo de 60 caracteres.
   - Debe contener palabras clave de búsqueda reales (ej. "Adaptador Magnetico USB C Tipo C Rapido").
   - NO debe contener palabras promocionales prohibidas (como "oferta", "descuento", "gratis", "promoción", "garantía", "envío", "barato", "inmediato", "stock", "nuevo").
   - NO debe incluir emojis ni caracteres especiales.
   - Debe ser descriptivo y seguir la estructura recomendada: Marca + Modelo + Nombre de Producto + Características clave.
2. **Extraer Atributos**:
   - Identifica la Marca (BRAND). Si no está clara o es genérica, usa "{vendor}" o "Genérico".
   - Identifica el Modelo (MODEL). Debe ser una palabra corta que identifique el modelo del producto.
3. **Optimizar la Descripción**:
   - Limpia todo el HTML y extrae solo texto plano legible.
   - Debe ser estructurada, limpia y profesional. Sin enlaces externos, ni menciones a Shopify o redes sociales.

Devuelve la respuesta estrictamente en formato JSON con la siguiente estructura exacta:
{{
  "optimized_title": "...",
  "brand": "...",
  "model": "...",
  "clean_description": "..."
}}
"""

    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            # Parsear y retornar el JSON
            return json.loads(response.text)
        except Exception as e:
            if attempt < max_retries - 1 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "ResourceExhausted" in str(e)):
                wait_time = 2 * (attempt + 1)
                print(f"  [Gemini 503] Servicio ocupado. Reintentando en {wait_time}s... (Intento {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                raise e
    raise Exception("No se pudo obtener respuesta de Gemini tras varios reintentos debido a la alta demanda.")


def extract_attributes_with_ai(title, description, required_attributes):
    """
    Usa la API de Gemini para extraer los valores de una lista de atributos requeridos
    a partir del título y la descripción del producto.
    """
    if not GEMINI_API_KEY:
        raise ValueError("Falta la variable GEMINI_API_KEY en tu archivo .env.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Construir la descripción de los atributos para el prompt
    attributes_desc = ""
    for attr in required_attributes:
        # Evitar sobreescribir BRAND o MODEL si ya los manejamos
        if attr['id'] in ['BRAND', 'MODEL']:
            continue
            
        allowed_values_str = ""
        if "values" in attr and attr["values"]:
            allowed_names = [v["name"] for v in attr["values"][:15]]  # Limitar para no inflar el prompt
            allowed_values_str = f" (Valores permitidos sugeridos: {', '.join(allowed_names)})"
            
        attributes_desc += f"- {attr['id']} ({attr['name']}): {attr.get('value_type', 'string')}{allowed_values_str}\n"

    # Si no hay atributos adicionales requeridos, retornamos una lista vacía
    if not attributes_desc.strip():
        return []

    prompt = f"""
Eres un catalogador experto en comercio electrónico. Tu trabajo es analizar la información de un producto y extraer los valores técnicos correspondientes para los siguientes atributos requeridos por Mercado Libre Chile:

Información del producto:
- Título: {title}
- Descripción: {description}

Atributos requeridos a extraer:
{attributes_desc}

Instrucciones para extraer:
1. Encuentra el valor correspondiente en el título o descripción del producto.
2. Si el atributo especifica una lista de "valores permitidos sugeridos" y el valor del producto es similar, utiliza exactamente ese nombre de la lista.
3. Si el producto no menciona información sobre ese atributo, deduce un valor genérico lógico (ejemplo: "Genérico", "No aplica", "Negro", "Universal") o selecciona el más básico de la lista de permitidos.
4. El valor debe ser directo y conciso (ejemplo: "USB", "Tipo C", "10W").

Devuelve la respuesta estrictamente en formato JSON plano con la siguiente estructura exacta:
{{
  "atributos": [
     {{
       "id": "ID_DEL_ATRIBUTO",
       "value_name": "VALOR_EXTRAIDO"
     }}
  ]
}}
"""

    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            return data.get("atributos", [])
        except Exception as e:
            if attempt < max_retries - 1 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "ResourceExhausted" in str(e)):
                wait_time = 2 * (attempt + 1)
                print(f"  [Gemini 503] Servicio ocupado. Reintentando extracción en {wait_time}s... (Intento {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"Error al procesar la extracción de atributos con la IA: {e}")
                return []
    return []


if __name__ == "__main__":

    test_title = "Adaptador magnético USB C"
    test_type = "adaptador"
    test_vendor = "Zipp Chile"
    test_desc = "<p>Adaptador magnético tipo C para carga rápida y transferencia de datos. ¡Compra el tuyo con envío gratis!</p>"
    
    try:
        print("Ejecutando prueba local de IA con gemini-2.5-flash en modo JSON...")
        optimized = optimize_product_with_ai(test_title, test_type, test_vendor, test_desc)
        print("\nResultado de Gemini:")
        print(json.dumps(optimized, indent=4, ensure_ascii=False))
    except Exception as e:
        print(f"Error en la prueba de IA: {e}")
