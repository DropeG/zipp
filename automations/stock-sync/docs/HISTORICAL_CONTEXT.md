# Stock Sync Context

## Objetivo

Construir un sincronizador de stock separado de la skill de publicación Shopify -> Mercado Libre.

Shopify es la fuente digital de verdad para productos y stock. La prioridad es evitar sobreventa en Mercado Libre cuando una venta ocurre en Shopify o Mercado Libre.

## Estado Actual

La V1 local de Shopify -> Mercado Libre ya funciona end-to-end.

Flujo validado:

```text
Venta Shopify
  -> webhook orders/create
  -> catcher local
  -> SQLite
  -> procesador dry-run
  -> --apply
  -> stock Meli actualizado
  -> tarea synced
```

Archivos principales:

```text
automations/stock-sync/scripts/shopify_webhook_catcher.js
automations/stock-sync/scripts/process_shopify_to_meli_stock.py
data/stock_sync.db
```

## Shopify -> Meli

### Catcher

Archivo:

```text
automations/stock-sync/scripts/shopify_webhook_catcher.js
```

Endpoint Shopify:

```text
POST /webhooks/shopify/orders-create
```

Hace:

```text
1. Recibe webhook orders/create.
2. Guarda raw event en SQLite.
3. Crea una stock_task por line item.
4. No duplica tareas si llega el mismo webhook/orden otra vez.
```

### Procesador

Archivo:

```text
automations/stock-sync/scripts/process_shopify_to_meli_stock.py
```

Dry-run:

```bash
python3 automations/stock-sync/scripts/process_shopify_to_meli_stock.py
```

Hace:

```text
1. Lee tareas pending.
2. Consulta stock real actual en Shopify.
3. Busca el mismo SKU en Meli.
4. No toca Meli.
5. Si todo está claro, deja la tarea ready_to_apply.
```

Apply:

```bash
python3 automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply
```

Hace:

```text
1. Lee tareas ready_to_apply.
2. Actualiza Meli available_quantity = stock actual Shopify.
3. Confirma stock en Meli.
4. Marca la tarea synced si salió bien.
```

## Reglas Confirmadas

```text
Shopify es la fuente de verdad.
SKU es la identidad compartida entre Shopify y Meli.
Mismo producto = mismo SKU.
Producto distinto = SKU distinto.
Si SKU no existe en Meli, no tocar Meli.
Si SKU aparece duplicado en Meli, mandar a revisión humana.
Si hay duda, no tocar stock.
No inventar stock.
No convertir stock 0 en 1.
```

## SKU En Mercado Libre

Al principio se buscó mal el SKU en Meli.

Campo incorrecto/incompleto:

```text
seller_custom_field
```

Campo real usado por muchas publicaciones actuales:

```text
attributes.SELLER_SKU
```

El procesador fue corregido para buscar en ambos:

```text
seller_custom_field
attributes.SELLER_SKU
```

## Prueba Real Validada

Orden Shopify:

```text
#1281
```

SKU:

```text
ADA-LEN-54
```

Resultado:

```text
Shopify stock: 1
Meli item: MLC2076811149
Meli stock antes: 2
Meli stock después: 1
Estado tarea: synced
```

Esto confirmó que Shopify -> Meli funciona localmente.

## Pendiente Para Producción

```text
Subir a servidor.
Mantener catcher corriendo.
Ejecutar procesador automáticamente.
Agregar logs/alertas.
Agregar reconciliación periódica.
```

## Meli -> Shopify

La V1 local de Meli -> Shopify ya funciona end-to-end con descuento directo de inventario en Shopify.

Decisión V1:

```text
Venta Meli
  -> leer orden Meli
  -> crear stock_task source=meli por línea
  -> dry-run calcula stock objetivo en Shopify
  -> --apply descuenta inventario Shopify
  -> confirma stock Shopify
  -> tarea synced
```

No crea una orden espejo en Shopify todavía. Eso queda como mejora futura porque puede disparar emails, webhooks, fulfillment, analytics o integraciones contables.

Endpoint agregado al catcher:

```text
POST /webhooks/meli/orders
```

URL configurada/probada con Cloudflare Tunnel:

```text
https://float-woods-read-furnished.trycloudflare.com/webhooks/meli/orders
```

La prueba falsa de notificación Meli funcionó:

```text
Raw event Meli guardado
Topic: orders_v2
Resource: /orders/987654321
Order ID: 987654321
User ID: 1604295292
```

Bloqueo histórico ya resuelto: el 2026-08-04 Mercado Libre no dejaba leer órdenes por API.

Error de ese momento:

```text
403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES
```

Probado con:

```text
GET /orders/search?seller=1604295292
GET /orders/search/recent
GET /orders/{id}
```

El token de ese momento sí podía leer:

```text
users/me
items
```

Pero no órdenes. El 2026-08-25 se confirmó lectura de órdenes reales y apply exitoso hacia Shopify.

### Comandos

Chequeo de permisos Meli:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --check-permissions
```

Dry-run de una orden puntual:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682
```

Apply de una orden puntual ya revisada:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682 --apply --limit 1
```

Procesar webhooks Meli guardados localmente:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
```

Apply de tareas Meli ready_to_apply:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10
```

### Prueba Real Validada 2026-08-25

Orden Meli:

```text
2000018107143682
```

SKU:

```text
SOP-BAS-58
```

Resultado:

```text
Meli item: MLC4243842760
Cantidad vendida: 1
Shopify variant: 41798285164679
Shopify inventory_item_id: 43895215915143
Shopify stock antes: 2
Shopify stock después: 1
Estado tarea: synced
```

Validación de idempotencia:

```text
Reprocesar la misma orden detectó la tarea existente y no dejó tareas pending.
No se descontó stock por segunda vez.
```

### Comportamiento Seguro

```text
Solo procesa órdenes Meli paid.
SKU es obligatorio para tocar Shopify.
El SKU debe existir exactamente una vez en Shopify.
Si el SKU no existe, no toca Shopify.
Si el SKU está duplicado, manda a revisión humana.
Si la variante no gestiona inventario con Shopify, manda a revisión humana.
El apply re-lee stock fresco antes de descontar.
El target nunca baja de 0.
Cada transición queda en sync_logs.
```

### Diagnóstico Histórico 2026-08-04

Se agregó un procesador dry-run inicial:

```text
automations/stock-sync/scripts/process_meli_to_shopify_stock.py
```

Comandos:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --check-permissions
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 1
```

Resultado de ese momento:

```text
users/me: OK
orders/search: HTTP 403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES
orders/search/recent: HTTP 403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES
```

Dry-run sobre el raw event Meli existente:

```text
Modo: dry-run Meli -> Shopify (no modifica Shopify)
Eventos Meli encontrados: 1

Procesando orden Meli 987654321
  -> bloqueado por permisos: Meli bloqueo la lectura de ordenes. Revisa en DevCenter el permiso funcional 'Ventas y envios' para orders/shipments y reautoriza el token.
```

El token actual trae scopes de publicacion/sincronizacion, pero no se ve un permiso funcional de ventas/envios en los scopes:

```text
offline_access read ... publish-sync ... write
```

La documentacion actual de Mercado Libre indica que:

```text
Ventas y envios permite acceso a orders, shipments, claims y returns.
PA_UNAUTHORIZED_RESULT_FROM_POLICIES aparece cuando falta el permiso funcional correspondiente.
```

Conclusión de ese momento: antes de crear orden/ajuste en Shopify había que habilitar el permiso funcional de Ventas y envios en DevCenter para la app correcta y reautorizar para generar token nuevo.

## App Meli Correcta

La app correcta en DevCenter es:

```text
Pedro Gonzalez
Client ID: 6682972689902515
```

Coincide con:

```text
MELI_APP_ID=6682972689902515
```

Se reautorizó token correctamente y quedó:

```text
Usuario ID: 1604295292
Nickname: ZIPPCHILESPA
País: MLC
```

El 2026-08-25 `orders/search` ya respondió OK y `GET /orders/{id}` pudo leer órdenes reales.

## Próximo Paso Recomendado En Nueva Conversación

Para producción:

```text
1. Subir catcher y procesadores a servidor.
2. Mantener webhook Meli activo y apuntando al endpoint estable.
3. Ejecutar dry-run y apply automáticamente con límites pequeños.
4. Agregar alertas para needs_review/retryable_error.
5. Diseñar una V2 opcional para crear orden espejo en Shopify si se necesita reporting centralizado.
```

Para retomar en una conversación nueva:

```text
Lee automations/stock-sync/docs/HISTORICAL_CONTEXT.md y continuemos desde Meli -> Shopify.
```
