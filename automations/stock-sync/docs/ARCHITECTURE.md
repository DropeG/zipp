# Arquitectura: Stock Sync

## Principios

```text
Shopify es la fuente digital de verdad para productos y stock.
SKU es la identidad compartida entre Shopify y Mercado Libre.
Si hay duda, no se toca stock.
```

El sincronizador usa dos fases:

- `dry-run`: lee datos, resuelve SKU, calcula resultado y deja tareas listas.
- `apply`: re-lee stock fresco y modifica inventario real.

## Componentes

```text
automations/stock-sync/scripts/shopify_webhook_catcher.js
  Servidor HTTP para webhooks Shopify y Meli.

automations/stock-sync/scripts/process_shopify_to_meli_stock.py
  Procesa ventas Shopify y actualiza stock Meli.

automations/stock-sync/scripts/process_meli_to_shopify_stock.py
  Procesa ventas Meli y descuenta stock Shopify.

data/stock_sync.db
  SQLite local/servidor. Cola durable y auditoria.
```

## Shopify -> Mercado Libre

```text
Venta Shopify
  -> POST /webhooks/shopify/orders-create
  -> raw_events
  -> stock_tasks source=shopify
  -> dry-run:
       lee stock actual Shopify
       busca SKU exacto en Meli
       deja ready_to_apply si el match es seguro
  -> apply:
       actualiza available_quantity en Meli
       confirma stock
       marca synced
```

Reglas:

- Si el line item no tiene SKU, queda `needs_review`.
- Si no tiene `variant_id`, queda `skipped_no_shopify_variant`.
- Si el SKU no existe en Meli, queda `skipped_not_in_meli`.
- Si el SKU aparece duplicado en Meli, queda `needs_review`.
- V1 de `apply` solo actualiza publicaciones simples de Meli, no variaciones.

## Mercado Libre -> Shopify

```text
Venta Mercado Libre
  -> POST /webhooks/meli/orders
  -> raw_events
  -> processor lee /orders/{id}
  -> stock_tasks source=meli por linea pagada
  -> dry-run:
       resuelve SKU
       busca variante Shopify por SKU exacto
       calcula target_stock = max(stock_actual - cantidad_vendida, 0)
       deja ready_to_apply si el match es seguro
  -> apply:
       re-lee stock Shopify fresco
       descuenta cantidad vendida
       confirma stock
       marca synced
```

Reglas:

- Solo procesa ordenes Meli `paid`.
- Si falta SKU, queda `needs_review`.
- Si el SKU no existe en Shopify, queda `skipped_not_in_shopify`.
- Si el SKU esta duplicado en Shopify, queda `needs_review`.
- Si la variante no gestiona inventario con Shopify, queda `needs_review`.
- El target nunca baja de cero.
- Reprocesar la misma orden no descuenta de nuevo.

## SQLite

Tablas principales:

```text
raw_events
  Webhooks recibidos.

stock_tasks
  Cola idempotente de ajustes de stock.

sync_logs
  Bitacora de decisiones, errores y transiciones.

sku_cache
  Cache auxiliar usada por Shopify -> Meli.
```

Estados principales:

```text
pending                Recibida y pendiente de dry-run.
ready_to_apply         Dry-run exitoso, lista para aplicar.
synced                 Cambio aplicado y confirmado.
needs_review           Requiere revision humana; no toca stock.
retryable_error        Error temporal o API; puede reintentarse.
skipped_not_in_meli    SKU Shopify no existe en Meli.
skipped_not_in_shopify SKU Meli no existe en Shopify.
```

## Idempotencia

Las tareas usan ids estables. Ejemplos:

```text
shopify:<order_id>:<line_item_id>
meli:<order_id>:<line_index>:<item_id>:<variation_id-or-no-variation>
```

El catcher/procesador usa inserciones idempotentes para que webhooks repetidos o reprocesos manuales no generen doble descuento.

## Datos Runtime

Los scripts viven dentro de `automations/stock-sync/`, pero leen/escriben estado runtime en la raiz del repo:

```text
.env
meli_tokens.json
data/stock_sync.db
logs/*.log
```
