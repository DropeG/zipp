# Inactivas - notas del flujo manual

Objetivo: construir primero el flujo manual para revisar publicaciones inactivas o `under_review` en Mercado Libre Zipp y, cuando el paso a paso este probado, convertirlo en una skill generica llamada `inactivas`.

## Paso 1: conectarse a Mercado Libre por API

- Usar el helper existente `meli_client.py`.
- Ejecutar con el Python del repo, porque el Python global no tiene `requests` instalado:

```bash
venv/bin/python
```

- Validar la cuenta con:

```python
from meli_client import get_meli_user_me
user = get_meli_user_me()
```

- Cuenta validada:
  - `id`: `1604295292`
  - `nickname`: `ZIPPCHILESPA`
  - `site_id`: `MLC`

## Paso 2: buscar publicaciones inactivas / bajo revision

Primero se probaron filtros directos:

```http
GET /users/{user_id}/items/search?status=active
GET /users/{user_id}/items/search?status=paused
GET /users/{user_id}/items/search?status=under_review
GET /users/{user_id}/items/search?status=inactive
GET /users/{user_id}/items/search?status=closed
```

Resultado: el filtro directo `status=under_review` devolvio 0, pero al consultar todas las publicaciones y luego pedir los items en batch aparecieron publicaciones `under_review`.

Flujo mas confiable:

```http
GET /users/{user_id}/items/search?limit=100
GET /items?ids=MLC...,MLC...
```

Luego filtrar localmente:

```python
item["status"] == "under_review"
```

Publicaciones encontradas:

| ID | Titulo | Estado | Subestado |
| --- | --- | --- | --- |
| `MLC4212313724` | Cable Disco Duro A Usb-c Negro | `under_review` | `waiting_for_patch` |
| `MLC2076830367` | Cargador Multi Usb , Carga Rapida (100w) Gris | `under_review` | `forbidden` |

Otros items no activos, pero no parecen corresponder al boton de politicas:

| ID | Estado | Subestado |
| --- | --- | --- |
| `MLC2035140981` | `paused` | `out_of_stock` |
| `MLC1570581199` | `closed` | `deleted` |
| `MLC2057126085` | `closed` | vacio |

## Paso 3: investigar una publicacion `under_review`

Caso elegido: `MLC4212313724`, porque `waiting_for_patch` indica que podria haber algo corregible.

Datos relevantes del item:

- `status`: `under_review`
- `sub_status`: `["waiting_for_patch"]`
- `category_id`: `MLC440106`
- `domain_id`: `MLC-DATA_CABLES_AND_ADAPTERS`
- `user_product_id`: `MLCU4380808876`
- `title`: `Cable Disco Duro A Usb-c Negro`
- `family_name`: `Cable Disco Duro A Usb-c`
- `pictures_count`: 3

Endpoints revisados:

```http
GET /items/MLC4212313724
GET /items/MLC4212313724/description
GET /categories/MLC440106
GET /categories/MLC440106/attributes
GET /user-products/MLCU4380808876
```

Tambien se probaron endpoints de moderacion:

```http
GET /moderations/items/MLC4212313724
GET /items/MLC4212313724/health
```

Resultados:

- `/moderations/items/{item_id}` respondio `503` vacio.
- `/items/{item_id}/health` respondio `403` con `blocked_by: PolicyAgent`.
- `/items/{item_id}/shipping_options?...` tambien respondio `403` con `PolicyAgent`.

La documentacion indexada de Mercado Libre indica:

- `waiting_for_patch`: item oculto hasta que el usuario corrija la infraccion reportada.
- `forbidden`: item dado de baja por moderacion.

## Paso 4: validar payload sin modificar la publicacion

Se uso:

```http
POST /items/validate
```

Notas:

- Estas publicaciones usan formato `User Product`.
- El validador exige `family_name`; con solo `title` devuelve `body.required_fields`.
- Con `family_name`, el validador no devolvio una causa de politica del item, solo warnings de shipping:
  - `shipping.lost_me1_by_user`
  - en el segundo item tambien `item.shipping.mandatory_free_shipping`

## Paso 5: diagnostico de imagenes

Endpoint usado:

```http
POST /moderations/pictures/diagnostic
```

Payload requerido:

```json
{
  "picture_url": "IMAGE_URL_OR_BASE64_OR_PICTURE_ID",
  "context": {
    "category_id": "MLC440106",
    "title": "Cable Disco Duro A Usb-c Negro",
    "picture_type": "thumbnail"
  }
}
```

Resultado para `MLC4212313724`:

| Foto | Tipo probado | Resultado |
| --- | --- | --- |
| `709198-MLC113579031938_072026` | `thumbnail` | `minimum_size`: No cumple el tamano minimo, posicion y proporcion del producto |
| `629810-MLC113579004512_072026` | `product` | `text_logo`: Contiene logos y/o textos |
| `629796-MLC114853540643_072026` | `product` | Sin detecciones |

Nota: este diagnostico de imagenes fue util, pero no era la causa principal del bloqueo observado en el panel. En la columna "Estado y recomendaciones", Mercado Libre mostro para `MLC4212313724`: "Inactiva para revisar - Incluye datos de contacto".

## Paso 6: corregir datos de contacto

El panel de Mercado Libre fue mas especifico que la API: `MLC4212313724` estaba inactiva porque incluia datos de contacto.

Busqueda de campos con datos de contacto:

- Descripcion: `Marca: Zipp.cl.`
- Atributo `BRAND`: `Zipp.cl`
- User Product `BRAND`: `Zipp.cl`

Correccion aplicada:

- Cambiar `Zipp.cl` por `Zipp` en la descripcion.
- Cambiar atributo `BRAND` de `Zipp.cl` a `Zipp`.

Endpoints usados:

```http
PUT /items/MLC4212313724/description
PUT /items/MLC4212313724
```

Payload de descripcion:

```json
{
  "plain_text": "...\\nMarca: Zipp."
}
```

Payload de atributo:

```json
{
  "attributes": [
    {
      "id": "BRAND",
      "value_name": "Zipp"
    }
  ]
}
```

Resultado:

- `MLC4212313724` paso de `status=under_review`, `sub_status=["waiting_for_patch"]` a `status=active`, `sub_status=[]`.
- El `User Product` tambien quedo con `BRAND=Zipp`.
- La busqueda posterior no encontro campos con `.cl`, URL, email, telefono, WhatsApp, redes o texto de contacto.

## Pendiente para el siguiente paso

- Para futuras publicaciones, revisar primero el texto de la columna "Estado y recomendaciones" del panel si esta disponible.
- Si dice "Incluye datos de contacto", buscar patrones como `.cl`, `.com`, `www`, `http`, `@`, telefonos, WhatsApp, Instagram/Facebook/TikTok y direcciones en descripcion, titulo, atributos y User Product.
- No eliminar publicaciones automaticamente.
- Si no hay reparacion segura, preguntar al usuario antes de eliminar.
