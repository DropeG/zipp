# Troubleshooting: Stock Sync

## `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`

Significa que Mercado Libre bloqueo lectura de ordenes por permisos funcionales.

Revisar:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --check-permissions
```

Accion:

1. Revisar DevCenter de Mercado Libre.
2. Confirmar que la app tenga permiso funcional de ventas/envios.
3. Reautorizar la cuenta Zipp.
4. Regenerar `meli_tokens.json`.

## No Existe `meli_tokens.json`

Ejecutar:

```bash
./venv/bin/python shared/meli_client.py
```

Autorizar con la cuenta de Zipp y pegar el `code` de retorno.

## No Existe `data/stock_sync.db`

Primero levantar el catcher o recibir/procesar eventos:

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js
```

El catcher crea la base si no existe.

## Tareas `needs_review`

El sistema no toca stock cuando hay ambiguedad.

Casos comunes:

- SKU faltante.
- SKU duplicado.
- Variante Shopify sin manejo de inventario.
- Match en Meli que no es publicacion simple.
- Faltan datos suficientes para resolver item o variacion.

Consultar:

```sql
SELECT task_id, source, order_id, sku, human_note, updated_at
FROM stock_tasks
WHERE status = 'needs_review'
ORDER BY updated_at DESC;
```

## Tareas `retryable_error`

Suele ser temporal o de API:

- token expirado o refresh fallido
- rate limit
- conectividad
- error 5xx
- confirmacion de stock que no calza

Accion:

1. Revisar `sync_logs`.
2. Confirmar credenciales.
3. Reintentar dry-run o apply con `--limit` pequeno.

## SKU No Existe En El Otro Sistema

Estados posibles:

```text
skipped_not_in_meli
skipped_not_in_shopify
```

No es necesariamente error de sistema. Puede indicar que el producto no esta publicado, no fue creado en Shopify, o usa otro SKU.

## Orden Reprocesada

Reprocesar una orden no deberia descontar dos veces. Buscar por `order_id`:

```sql
SELECT task_id, source, order_id, sku, quantity_sold, status
FROM stock_tasks
WHERE order_id = 'ORDER_ID';
```

Si ya esta `synced`, no deberia volver a aplicar.

## Webhook No Llega

Revisar:

- dominio publico o tunnel activo
- puerto expuesto
- catcher corriendo
- ruta exacta del endpoint
- logs del servicio

Endpoints correctos:

```text
POST /webhooks/shopify/orders-create
POST /webhooks/meli/orders
```

## Stock Incorrecto Despues De Apply

Accion inmediata:

1. Detener procesadores automaticos.
2. Revisar `sync_logs`.
3. Buscar tarea por `task_id` u `order_id`.
4. Corregir stock manualmente en Shopify o Meli segun corresponda.
5. Mantener el registro en SQLite para auditoria.
