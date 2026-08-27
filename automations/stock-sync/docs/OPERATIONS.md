# Operacion: Stock Sync

Ejecutar comandos desde la raiz del repo.

## Arrancar Catcher

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js
```

## Dry-Run

Shopify -> Meli:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10
```

Meli -> Shopify:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
```

Orden Meli puntual:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682
```

## Apply

Shopify -> Meli:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 10
```

Meli -> Shopify:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10
```

Orden Meli puntual:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682 --apply --limit 1
```

## Inspeccion SQLite

```bash
sqlite3 data/stock_sync.db
```

Consultas utiles:

```sql
SELECT status, source, COUNT(*) FROM stock_tasks GROUP BY status, source;
SELECT * FROM stock_tasks ORDER BY updated_at DESC LIMIT 20;
SELECT * FROM sync_logs ORDER BY created_at DESC LIMIT 50;
SELECT * FROM raw_events ORDER BY received_at DESC LIMIT 20;
```

Ver solo tareas que requieren mirada humana:

```sql
SELECT task_id, source, order_id, sku, status, human_note, updated_at
FROM stock_tasks
WHERE status IN ('needs_review', 'retryable_error')
ORDER BY updated_at DESC;
```

## Logs

Crear carpeta:

```bash
mkdir -p logs
```

Cron recomendado:

```cron
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10 >> logs/shopify_to_meli.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 10 >> logs/shopify_to_meli_apply.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10 >> logs/meli_to_shopify.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10 >> logs/meli_to_shopify_apply.log 2>&1
```

Si se usa `systemd` para el catcher:

```bash
journalctl -u zipp-stock-webhook-catcher -f
```

## Backup SQLite

Backup manual:

```bash
sqlite3 data/stock_sync.db ".backup 'data/stock_sync.db.backup-$(date +%Y%m%d-%H%M%S)'"
```

Tambien se puede copiar el archivo con el catcher y procesadores detenidos.

## Ritmo Operativo Recomendado

1. Revisar que el catcher este activo.
2. Revisar logs de dry-run y apply.
3. Consultar conteo de tareas por estado.
4. Resolver `needs_review` manualmente.
5. Reintentar `retryable_error` despues de validar permisos/conectividad.
6. Mantener backup periodico de `data/stock_sync.db`.
