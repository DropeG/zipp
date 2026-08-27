import os
import json
import argparse
import requests
import time
import mimetypes
from urllib.parse import urlparse
from meli_client import get_meli_headers, publish_meli_item, check_meli_item_exists, update_meli_item_description, update_meli_item_price_and_stock

def load_json_file(filename):
    if not os.path.exists(filename):
        print(f"Error: No se encuentra el archivo {filename}.")
        return []
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def load_json_object(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json_object(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def record_blocked_product(shopify_id, title, meli_item_id, final_item, reason):
    filename = "sync_blocked_products.json"
    blocked_products = load_json_object(filename)
    current = blocked_products.get(shopify_id, {})
    meli_item_ids = current.get("meli_item_ids") or []
    if meli_item_id and meli_item_id not in meli_item_ids:
        meli_item_ids.append(meli_item_id)

    blocked_products[shopify_id] = {
        "title": current.get("title") or title,
        "blocked_at": time.strftime("%Y-%m-%d"),
        "reason": reason,
        "meli_item_ids": meli_item_ids,
        "last_status": final_item.get("status"),
        "last_sub_status": final_item.get("sub_status") or [],
        "do_not_auto_republish": True,
    }
    save_json_object(filename, blocked_products)

def upload_original_pictures_to_meli(image_urls):
    from meli_client import get_meli_access_token

    upload_headers = {"Authorization": f"Bearer {get_meli_access_token()}"}
    picture_ids = []
    for idx, image_url in enumerate(image_urls, start=1):
        image_response = requests.get(image_url, timeout=60)
        image_response.raise_for_status()
        content_type = image_response.headers.get("content-type") or "image/jpeg"
        extension = mimetypes.guess_extension(content_type.split(";")[0]) or os.path.splitext(urlparse(image_url).path)[1] or ".jpg"
        files = {
            "file": (f"shopify_image_{idx}{extension}", image_response.content, content_type)
        }
        upload_response = requests.post(
            "https://api.mercadolibre.com/pictures/items/upload",
            headers=upload_headers,
            files=files,
            timeout=120,
        )
        upload_response.raise_for_status()
        picture_id = upload_response.json().get("id")
        if not picture_id:
            raise RuntimeError(f"Mercado Libre did not return a picture id: {upload_response.text}")
        picture_ids.append(picture_id)
    return picture_ids

def ensure_item_active(item_id, image_urls, attempts=8, delay_seconds=10):
    headers = get_meli_headers()
    uploaded_pending_pictures = False

    for attempt in range(1, attempts + 1):
        response = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=30)
        response.raise_for_status()
        item = response.json()
        status = item.get("status")
        sub_status = item.get("sub_status") or []

        if status == "active":
            print(f"  -> Publicación activa verificada: {item_id}")
            return item

        print(f"  -> Esperando activación de {item_id}: status={status}, sub_status={sub_status} (intento {attempt}/{attempts})")

        if "picture_download_pending" in sub_status and not uploaded_pending_pictures:
            print("  -> Subiendo imágenes originales de Shopify a Mercado Libre picture hosting...")
            picture_ids = upload_original_pictures_to_meli(image_urls)
            requests.put(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers=headers,
                json={"pictures": [{"id": picture_id} for picture_id in picture_ids]},
                timeout=60,
            ).raise_for_status()
            uploaded_pending_pictures = True

        if status in ["paused", "under_review"]:
            requests.put(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers=headers,
                json={"status": "active"},
                timeout=60,
            )

        if attempt < attempts:
            time.sleep(delay_seconds)

    response = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=30)
    response.raise_for_status()
    item = response.json()
    print(f"  [ADVERTENCIA] La publicación no quedó activa: status={item.get('status')}, sub_status={item.get('sub_status')}")
    return item

def run_publication(dry_run=True):
    print("==========================================================")
    print(f"INICIANDO PUBLICACIÓN DE PAYLOADS OPTIMIZADOS POR EL AGENTE")
    print(f"Modo Prueba: {dry_run}")
    print("==========================================================")

    # Cargar mapeos locales para evitar duplicados
    from sync_products import load_mappings, save_mappings, build_meli_payload, validate_item_on_meli
    mappings = load_mappings()
    blocked_products = load_json_object("sync_blocked_products.json")

    productos_listos = load_json_file("productos_listos.json")
    if not productos_listos:
        print("No hay productos listos para publicar.")
        return

    print(f"Se procesarán {len(productos_listos)} productos.")

    for idx, p in enumerate(productos_listos, start=1):
        shopify_id = str(p["shopify_id"])
        title = p["ai_data"]["optimized_title"]
        category_id = p["category_id"]
        price = p["price"]
        stock = p["stock"]
        if stock <= 0:
            stock = 1  # Stock mínimo para evitar errores de validación de cero stock
        barcode = p.get("barcode", "")
        meli_pictures = [{"source": url} for url in p["images"]]
        ai_data = p["ai_data"]
        extra_attributes = p.get("extra_attributes", [])
        shipping = p.get("shipping")
        variations = p.get("variations")

        print(f"\n----------------------------------------------------------")
        print(f"Procesando [{idx}/{len(productos_listos)}]: '{title}' (ID Shopify: {shopify_id})")
        print(f"----------------------------------------------------------")

        blocked = blocked_products.get(shopify_id)
        if blocked and blocked.get("do_not_auto_republish", True):
            print(f"  [BLOQUEADO] No se republica automáticamente: {blocked.get('reason', 'sin razón registrada')}")
            continue

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

        # Validación adaptativa
        use_catalog = False
        print("  -> Validando publicación...")
        meli_item = build_meli_payload(ai_data, category_id, stock, price, meli_pictures, extra_attributes, barcode=barcode, use_catalog_format=False, shipping=shipping, variations=variations)
        response = validate_item_on_meli(meli_item)

        is_valid = False
        if response.status_code in [200, 204]:
            is_valid = True
        elif response.status_code == 400:
            res_json = response.json()
            causes = res_json.get("cause", [])
            errors = [c for c in causes if c.get("type") == "error"]
            warnings = [c for c in causes if c.get("type") == "warning"]

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
                meli_item = build_meli_payload(ai_data, category_id, stock, price, meli_pictures, extra_attributes, barcode=barcode, use_catalog_format=True, shipping=shipping, variations=variations)
                response = validate_item_on_meli(meli_item)

                if response.status_code in [200, 204]:
                    is_valid = True
                elif response.status_code == 400:
                    res_json = response.json()
                    errors = [c for c in res_json.get("cause", []) if c.get("type") == "error"]
                    warnings = [c for c in res_json.get("cause", []) if c.get("type") == "warning"]
                    variation_family_error = any(
                        "variations is invalid with family name" in err.get("message", "")
                        for err in errors
                    )
                    if variation_family_error and variations:
                        print("  [Auto-Corrección] Mercado Libre rechazó variaciones con family_name. Re-intentando como publicación general sin color en el título...")
                        variations = None
                        meli_item = build_meli_payload(ai_data, category_id, stock, price, meli_pictures, extra_attributes, barcode=barcode, use_catalog_format=True, shipping=shipping, variations=variations)
                        response = validate_item_on_meli(meli_item)
                        if response.status_code in [200, 204]:
                            is_valid = True
                        elif response.status_code == 400:
                            res_json = response.json()
                            errors = [c for c in res_json.get("cause", []) if c.get("type") == "error"]
                            warnings = [c for c in res_json.get("cause", []) if c.get("type") == "warning"]
                            if warnings and not errors:
                                is_valid = True
                    if warnings and not errors:
                        is_valid = True
            elif warnings and not errors:
                is_valid = True

        if not is_valid:
            print(f"  [ERROR] Falló la validación final en Mercado Libre (HTTP {response.status_code}):")
            print(f"  {response.text}")
            continue

        # Publicación real o simulación
        if dry_run:
            print(f"  [MODO PRUEBA] ¡Listo para publicar en vivo! Formato: {'Catálogo' if use_catalog else 'Estándar'}")
        else:
            print(f"  -> Publicando en Mercado Libre de forma real...")
            try:
                result = publish_meli_item(meli_item)
                meli_item_id = result.get("id")
                print(f"  [PUBLICADO] ¡Sincronizado! ID: {meli_item_id}")
                
                # Subir descripción en texto plano
                update_meli_item_description(meli_item_id, ai_data["clean_description"])

                final_item = ensure_item_active(meli_item_id, p["images"])
                if final_item.get("status") == "active":
                    # Guardar mapeo solo después de verificar que la publicación quedó activa.
                    mappings[shopify_id] = meli_item_id
                    save_mappings(mappings)
                else:
                    reason = (
                        "Mercado Libre did not keep the listing active after safe activation checks; "
                        f"final status={final_item.get('status')}, sub_status={final_item.get('sub_status')}"
                    )
                    record_blocked_product(shopify_id, title, meli_item_id, final_item, reason)
                    print(f"  [BLOQUEADO] {reason}")
            except Exception as e:
                print(f"  [ERROR] Falló la publicación final: {e}")

    print("\n==========================================================")
    print("PROCESO DE PUBLICACIÓN FINALIZADO")
    print("==========================================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publicador de payloads de Mercado Libre.")
    parser.add_argument("--publish", action="store_true", help="Realiza la publicación en vivo (por defecto corre en modo prueba).")
    args = parser.parse_args()
    
    run_publication(dry_run=not args.publish)
