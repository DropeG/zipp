# Stock Sync Shopify <-> Mercado Libre

Automatizacion que sincroniza stock entre Shopify y Mercado Libre Chile usando webhooks, una cola SQLite local y procesadores con modo seguro `dry-run` antes de aplicar cambios reales.

## Estado

Validado localmente:

- Shopify -> Mercado Libre funciona end-to-end.
- Mercado Libre -> Shopify funciona end-to-end.
- Las tareas son idempotentes: reprocesar la misma orden no descuenta dos veces.
- El modo `apply` vuelve a leer stock fresco antes de modificar inventario.

Pendiente principal: montaje en servidor, dominio estable para webhooks, automatizacion recurrente, backups y alertas.

## Modelo Mental

```text
Shopify = fuente digital de verdad para productos y stock
SKU     = identidad compartida entre Shopify y Mercado Libre
SQLite  = cola durable y bitacora local del sincronizador
dry-run = valida y prepara
apply   = modifica stock real
```

## Flujos

```text
Venta Shopify
  -> webhook Shopify
  -> stock_tasks source=shopify
  -> dry-run
  -> apply
  -> stock Mercado Libre actualizado
```

```text
Venta Mercado Libre
  -> webhook Meli
  -> lectura de orden por API
  -> stock_tasks source=meli
  -> dry-run
  -> apply
  -> stock Shopify actualizado
```

## Scripts

```text
scripts/shopify_webhook_catcher.js
scripts/process_shopify_to_meli_stock.py
scripts/process_meli_to_shopify_stock.py
```

Ejecutar desde la raiz del repo:

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js

./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 10

./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10
```

## Documentacion

- `docs/HANDOFF.md`: pasos para montar en servidor.
- `docs/ARCHITECTURE.md`: como funciona internamente.
- `docs/OPERATIONS.md`: operacion diaria, logs, SQLite y backups.
- `docs/LOCAL_TESTING.md`: como probar localmente imitando produccion.
- `docs/TROUBLESHOOTING.md`: problemas comunes y recuperacion.
- `docs/PENDING.md`: pendientes y mejoras futuras.
- `docs/HISTORICAL_CONTEXT.md`: bitacora historica validada durante desarrollo.
