import argparse
import mimetypes
import os
import tempfile
from pathlib import Path

import requests

from meli_client import get_meli_access_token, get_meli_headers


PHOTOROOM_URL = "https://sdk.photoroom.com/v1/segment"
REMOVE_BG_URL = "https://api.remove.bg/v1.0/removebg"


def download_url(url, output_path):
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    output_path.write_bytes(response.content)
    return response.headers.get("content-type") or mimetypes.guess_type(url)[0] or "image/jpeg"


def process_with_photoroom(input_path, output_path):
    api_key = os.getenv("PHOTOROOM_API_KEY")
    if not api_key:
        return False, "PHOTOROOM_API_KEY is not configured"

    with input_path.open("rb") as image_file:
        response = requests.post(
            PHOTOROOM_URL,
            headers={"x-api-key": api_key},
            files={"image_file": image_file},
            data={
                "bg_color": "FFFFFF",
                "format": "jpg",
                "size": "full",
            },
            timeout=180,
        )
    if response.ok:
        output_path.write_bytes(response.content)
        return True, "processed with PhotoRoom"
    return False, f"PhotoRoom failed: HTTP {response.status_code} {response.text[:500]}"


def process_with_remove_bg(input_path, output_path):
    api_key = os.getenv("REMOVE_BG_API_KEY")
    if not api_key:
        return False, "REMOVE_BG_API_KEY is not configured"

    with input_path.open("rb") as image_file:
        response = requests.post(
            REMOVE_BG_URL,
            headers={"X-Api-Key": api_key},
            files={"image_file": image_file},
            data={
                "size": "auto",
                "bg_color": "FFFFFF",
                "format": "jpg",
            },
            timeout=180,
        )
    if response.ok:
        output_path.write_bytes(response.content)
        return True, "processed with remove.bg"
    return False, f"remove.bg failed: HTTP {response.status_code} {response.text[:500]}"


def process_cover(input_path, output_path):
    messages = []
    for processor in (process_with_photoroom, process_with_remove_bg):
        ok, message = processor(input_path, output_path)
        messages.append(message)
        if ok:
            return message
    raise RuntimeError("; ".join(messages))


def upload_picture(image_path):
    headers = {"Authorization": f"Bearer {get_meli_access_token()}"}
    content_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with image_path.open("rb") as image_file:
        response = requests.post(
            "https://api.mercadolibre.com/pictures/items/upload",
            headers=headers,
            files={"file": (image_path.name, image_file, content_type)},
            timeout=180,
        )
    response.raise_for_status()
    picture_id = response.json().get("id")
    if not picture_id:
        raise RuntimeError(f"Mercado Libre did not return a picture id: {response.text[:500]}")
    return picture_id


def fix_item_cover(item_id, publish=False):
    headers = get_meli_headers()
    item_response = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=60)
    item_response.raise_for_status()
    item = item_response.json()
    pictures = item.get("pictures") or []
    if not pictures:
        raise RuntimeError(f"{item_id} has no pictures")

    cover_url = pictures[0].get("secure_url") or pictures[0].get("url")
    if not cover_url:
        raise RuntimeError(f"{item_id} cover has no URL")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        source_path = temp_path / f"{item_id}_cover_source.jpg"
        output_path = temp_path / f"{item_id}_cover_white.jpg"
        download_url(cover_url, source_path)
        provider_message = process_cover(source_path, output_path)

        if not publish:
            print(f"[DRY-RUN] {item_id}: {provider_message}. Output ready at temporary path {output_path}")
            print("[DRY-RUN] Re-run with --publish to upload and set as cover.")
            return

        new_picture_id = upload_picture(output_path)
        existing_picture_ids = [pic.get("id") for pic in pictures if pic.get("id")]
        updated_pictures = [{"id": new_picture_id}] + [{"id": pic_id} for pic_id in existing_picture_ids]
        update_response = requests.put(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=headers,
            json={"pictures": updated_pictures},
            timeout=90,
        )
        update_response.raise_for_status()

    final = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=60)
    final.raise_for_status()
    final_item = final.json()
    print(
        f"{item_id}: cover updated via {provider_message}; "
        f"status={final_item.get('status')}; sub_status={final_item.get('sub_status')}; "
        f"warnings={final_item.get('warnings')}"
    )


def main():
    parser = argparse.ArgumentParser(description="Replace Mercado Libre item cover using an approved external white-background provider.")
    parser.add_argument("item_ids", nargs="+", help="Mercado Libre item ids, e.g. MLC4139259390")
    parser.add_argument("--publish", action="store_true", help="Upload the processed cover and update the Mercado Libre item.")
    args = parser.parse_args()

    for item_id in args.item_ids:
        try:
            fix_item_cover(item_id, publish=args.publish)
        except Exception as exc:
            print(f"{item_id}: cover not updated: {exc}")


if __name__ == "__main__":
    main()
