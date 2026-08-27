# Pruebas Locales: Stock Sync

Manual para probar el sincronizador Shopify <-> Mercado Libre desde un computador local imitando produccion.

La idea es validar el flujo real con webhooks reales, SQLite local, dry-run y, solo si corresponde, un apply manual limitado.

## Objetivo

Probar que la automatizacion puede:

- recibir webhooks desde Shopify o Mercado Libre
- guardar eventos en `data/stock_sync.db`
- crear o procesar tareas de stock
- ejecutar dry-run sin tocar stock real
- aplicar un cambio real controlado cuando el humano lo decide

## Flujo De Prueba

```text
Shopify / Mercado Libre
        |
        v
URL publica temporal de cloudflared
        |
        v
http://localhost:3000
        |
        v
webhook_catcher.js
        |
        v
data/stock_sync.db
        |
        v
dry-run
        |
        v
apply manual limitado
```

## Requisitos Locales

Ejecutar desde la raiz del repo.

Debe existir:

- `.env` con credenciales reales.
- `meli_tokens.json` generado con la cuenta correcta de Mercado Libre.
- `venv` con dependencias instaladas.
- Node.js 22 o superior.
- `cloudflared` instalado.
- Acceso a Shopify Admin.
- Acceso a la app de Mercado Libre usada por Zipp.

Instalacion base si todavia no existe:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
mkdir -p data logs
```

Editar `.env` con valores reales:

```text
SHOPIFY_SHOP_URL=...
SHOPIFY_ACCESS_TOKEN=...
SHOPIFY_API_VERSION=2024-04
MELI_APP_ID=...
MELI_CLIENT_SECRET=...
MELI_REDIRECT_URI=https://localhost
GEMINI_API_KEY=...
```

Generar `meli_tokens.json` si no existe:

```bash
./venv/bin/python shared/meli_client.py
```

Validar permisos basicos de Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --check-permissions
```

## 1. Levantar El Catcher Local

En una terminal:

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js
```

Debe mostrar algo parecido a:

```text
Escuchando en http://localhost:3000
Guardando datos en: .../data/stock_sync.db
```

Dejar esta terminal abierta mientras dure la prueba.

## 2. Abrir Un Tunel Publico Temporal

En otra terminal:

```bash
cloudflared tunnel --url http://localhost:3000
```

Cloudflared entregara una URL publica temporal parecida a:

```text
https://nombre-temporal.trycloudflare.com
```

Usar esa URL solo durante la prueba local. Si se cierra el tunnel, la URL deja de servir.

## 3. Configurar Webhooks Temporales

Usar la URL temporal de cloudflared para apuntar los webhooks al computador local.

Shopify:

```text
https://nombre-temporal.trycloudflare.com/webhooks/shopify/orders-create
```

Mercado Libre:

```text
https://nombre-temporal.trycloudflare.com/webhooks/meli/orders
```

Configurar solo los eventos necesarios para la prueba.

No dejar estos webhooks temporales activos despues de terminar. En produccion deben apuntar al dominio o tunnel estable del servidor.

## 4. Generar Un Evento De Prueba

Para Shopify -> Mercado Libre:

1. Elegir un producto que exista en Shopify y Mercado Libre con el mismo SKU.
2. Confirmar manualmente el stock actual en ambos sistemas.
3. Crear una venta/pedido de prueba en Shopify.
4. Esperar a que Shopify envie el webhook `orders/create`.

Para Mercado Libre -> Shopify:

1. Elegir una publicacion de Mercado Libre que tenga SKU equivalente en Shopify.
2. Confirmar manualmente el stock actual en ambos sistemas.
3. Generar o esperar una venta/notificacion de orden en Mercado Libre.
4. Esperar a que Mercado Libre envie el webhook de orden.

Usar productos de bajo riesgo o cantidades pequenas. Esta prueba puede terminar tocando stock real si luego se ejecuta `--apply`.

## 5. Confirmar Que Llego El Webhook

Abrir SQLite:

```bash
sqlite3 data/stock_sync.db
```

Ver eventos recientes:

```sql
SELECT id, source, topic, order_id, order_name, received_at
FROM raw_events
ORDER BY received_at DESC
LIMIT 10;
```

Resultado esperado:

- Para Shopify debe aparecer `source = 'shopify'`.
- Para Mercado Libre debe aparecer `source = 'meli'`.

## 6. Confirmar Tareas De Stock

Shopify crea tareas desde el catcher cuando llega el webhook:

```sql
SELECT task_id, source, order_id, order_name, sku, quantity_sold, status, updated_at
FROM stock_tasks
ORDER BY updated_at DESC
LIMIT 20;
```

Resultado esperado para Shopify:

- `source = 'shopify'`
- `status = 'pending'` si el SKU y la variante vienen bien
- `status = 'needs_review'` si falta SKU u otro dato critico

Para Mercado Libre, el webhook primero queda en `raw_events`. Las tareas se crean cuando se procesa el evento con el script Meli -> Shopify.

## 7. Ejecutar Dry-Run

Dry-run no modifica stock real. Sirve para revisar que la automatizacion entiende correctamente la tarea.

Shopify -> Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10
```

Mercado Libre -> Shopify:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
```

Orden puntual de Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id ORDER_ID
```

Revisar tareas despues del dry-run:

```sql
SELECT task_id, source, order_id, sku, quantity_sold, status, human_note, updated_at
FROM stock_tasks
ORDER BY updated_at DESC
LIMIT 20;
```

Resultado esperado:

- `ready_to_apply` si la automatizacion encontro una accion segura.
- `needs_review` si necesita mirada humana.
- `skipped_not_in_meli` o `skipped_not_in_shopify` si el SKU no existe en el otro sistema.

## 8. Aplicar Un Cambio Real Controlado

Aplicar solo despues de revisar el dry-run y confirmar manualmente que el cambio esperado es correcto.

Usar `--limit 1` para probar una sola tarea:

Shopify -> Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 1
```

Mercado Libre -> Shopify:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 1
```

Orden puntual de Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id ORDER_ID --apply --limit 1
```

Despues del apply:

1. Revisar stock en Shopify.
2. Revisar stock en Mercado Libre.
3. Revisar estado final en SQLite.

```sql
SELECT task_id, source, order_id, sku, quantity_sold, status, human_note, updated_at
FROM stock_tasks
ORDER BY updated_at DESC
LIMIT 20;
```

Resultado esperado:

- La tarea aplicada queda en `synced`.
- El stock real cambia solo en el sistema destino.
- Reprocesar la misma orden no debe descontar dos veces.

## 9. Limpieza Despues De Probar

Al terminar:

1. Detener el tunnel `cloudflared`.
2. Detener el catcher local.
3. Eliminar o desactivar webhooks temporales que apunten a `trycloudflare.com`.
4. Confirmar que no quedan procesadores corriendo en automatico.
5. Mantener `data/stock_sync.db` si se quiere conservar auditoria local de la prueba.

## Checklist De Prueba Local

- [ ] `.env` configurado.
- [ ] `meli_tokens.json` creado.
- [ ] `--check-permissions` de Mercado Libre revisado.
- [ ] Catcher local corriendo en `localhost:3000`.
- [ ] Tunnel `cloudflared` activo.
- [ ] Webhook temporal configurado.
- [ ] Evento recibido en `raw_events`.
- [ ] Tarea creada o procesada en `stock_tasks`.
- [ ] Dry-run ejecutado.
- [ ] Resultado revisado por humano.
- [ ] Apply manual con `--limit 1`, si corresponde.
- [ ] Stock final revisado en Shopify y Mercado Libre.
- [ ] Webhooks temporales eliminados o desactivados.
- [ ] Tunnel y catcher detenidos.
