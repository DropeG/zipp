# Stock Sync De Produccion

Este servicio recibe webhooks, los guarda en SQLite y un solo worker los procesa. Una venta pagada de Mercado Libre puede crear una orden pagada y etiquetada en Shopify; una orden de Shopify o la tarea diaria puede actualizar la cantidad de Mercado Libre. El receiver solo encola trabajo: no llama a Shopify ni a Mercado Libre.

Todos los comandos de este documento se ejecutan desde la raiz del repositorio. Se verifico esta version con Python 3.14.6 y Node.js 24.19.0. Node debe incluir `node:sqlite`; use Node.js 24.19.0 para la instalacion de produccion.

## Instalacion

```bash
python3.14 -m venv venv
./venv/bin/python -m pip install -r prod/stock-sync/requirements.txt
cp prod/stock-sync/.env.example prod/stock-sync/.env
```

Complete `prod/stock-sync/.env` y carguelo antes de iniciar el receiver o el worker:

```bash
set -a
. prod/stock-sync/.env
set +a
```

No suba ese archivo ni la base de datos. `prod/stock-sync/data/.gitkeep` mantiene el directorio vacio en Git.

## Credenciales Y Variables De Entorno

| Variable | Requerida | Uso |
| --- | --- | --- |
| `SHOPIFY_SHOP_URL` | Si | URL `https://<tienda>.myshopify.com` de la tienda. |
| `SHOPIFY_ACCESS_TOKEN` | Si | Token de la app Admin de Shopify. |
| `SHOPIFY_WEBHOOK_SECRET` | Si | Secreto para validar `orders/create` de Shopify. |
| `MELI_APP_ID` | Si | Client ID de la aplicacion de Mercado Libre. |
| `MELI_CLIENT_SECRET` | Si | Client secret de la aplicacion de Mercado Libre. |
| `MELI_EXPECTED_SELLER_ID` | Si | ID exacto del vendedor autorizado. El worker rechaza ordenes, listings y tokens de otro vendedor. |
| `MELI_WEBHOOK_TOKEN` | Si | Valor compartido que el receiver compara con el encabezado `x-webhook-token` de Mercado Libre. |
| `MELI_TOKENS_FILE` | No; tiene ruta por defecto | Ruta al archivo de tokens de Mercado Libre. Por defecto: `prod/stock-sync/data/meli_tokens.json`. |
| `STOCK_SYNC_DATABASE` | No; tiene ruta por defecto | Ruta SQLite. Por defecto: `prod/stock-sync/data/stock_sync.db`. |
| `SHOPIFY_API_VERSION` | No; tiene valor por defecto | Version Admin GraphQL. Por defecto: `2026-07`. |
| `MAX_WEBHOOK_BYTES` | No; tiene valor por defecto | Tamano maximo aceptado por webhook. Por defecto: `1048576`. |
| `PORT` | No; tiene valor por defecto | Puerto HTTP del receiver. Por defecto: `3000`. |

Tambien es obligatorio disponer de un archivo de tokens valido de Mercado Libre en `MELI_TOKENS_FILE`. Debe contener `access_token`, `refresh_token` y `expires_at` (timestamp Unix). El servicio refresca el token y reemplaza ese archivo de forma atomica. La obtencion inicial del token OAuth no tiene un comando en este repositorio: hagala con la aplicacion autorizada para el mismo `MELI_EXPECTED_SELLER_ID` y guarde solo el resultado en ese archivo.

La app de Shopify debe conceder exactamente estos scopes requeridos por el servicio: `read_products`, `read_inventory`, `read_orders`, `write_orders` y `write_draft_orders`. El acceso de Mercado Libre debe pertenecer al vendedor configurado y permitir leer el usuario, ordenes e items/listings, y actualizar la cantidad disponible de items. No hay un nombre de scope de Mercado Libre configurado en el codigo.

Configure en Shopify el webhook `orders/create` hacia `POST /webhooks/shopify/orders-create`. Configure en Mercado Libre las notificaciones de ordenes hacia `POST /webhooks/meli` con el encabezado `x-webhook-token` igual a `MELI_WEBHOOK_TOKEN`. El endpoint `GET /health` responde `ok`.

## Comandos

Primero cargue las variables de entorno indicadas arriba. Estos son los nombres reales de `worker.py`; no hay comandos `--apply`, `--order-id` ni `--limit` en este servicio.

| Objetivo | Comando | Efecto |
| --- | --- | --- |
| Crear o actualizar el esquema local | `./venv/bin/python prod/stock-sync/worker.py migrate` | Solo SQLite; no llama APIs. |
| Ejecutar pruebas Python | `./venv/bin/python -m pytest prod/stock-sync/tests -q` | Pruebas aisladas con clientes falsos y SQLite temporal. |
| Ejecutar pruebas Node | `(cd prod/stock-sync && node --test tests/test_receiver.js)` | Receiver local y archivos temporales del sistema, eliminados por la prueba. |
| Iniciar receiver | `node prod/stock-sync/receiver.js` | Escucha webhooks y encola trabajo. |
| Inspeccionar un solo trabajo siguiente | `./venv/bin/python prod/stock-sync/worker.py once --dry-run` | Lee las APIs para evaluar el siguiente trabajo, sin crear ordenes ni actualizar stock. |
| Aplicar un solo trabajo siguiente | `./venv/bin/python prod/stock-sync/worker.py once` | Puede crear una orden Shopify o actualizar stock Mercado Libre. Requiere confirmacion explicita previa. |
| Worker continuo | `./venv/bin/python prod/stock-sync/worker.py run` | Aplica cada trabajo disponible. No lo inicie antes de las verificaciones en vivo. |
| Revision diaria sin cambios | `./venv/bin/python prod/stock-sync/worker.py daily --dry-run` | Lee ordenes y catalogos, muestra importaciones y cantidades propuestas; no los cambia. |
| Revision diaria aplicada | `./venv/bin/python prod/stock-sync/worker.py daily` | Puede importar ordenes y actualizar cantidades Mercado Libre. Requiere confirmacion explicita previa. |
| Listar trabajos en revision | `./venv/bin/python prod/stock-sync/worker.py list-review` | Muestra JSON de los trabajos `needs_review`, sin requerir credenciales API. |
| Reintentar un trabajo revisado | `./venv/bin/python prod/stock-sync/worker.py retry JOB_ID` | Devuelve solo ese trabajo `needs_review` a `pending`; no llama APIs. |

`once --dry-run` toma el primer trabajo elegible. El CLI no permite elegir una orden Mercado Libre por ID. Para una prueba controlada, asegure que la nueva cola contiene solo el webhook de la orden revisada antes de ejecutar ese comando. Si hay mas trabajos pendientes, no ejecute `once` hasta aislar la cola.

## Operacion Normal

Inicie el receiver y el worker como procesos separados. El receiver debe permanecer accesible para los dos proveedores; el worker usa un bloqueo SQLite, por lo que solo puede procesar un trabajo o una revision diaria a la vez. Revise regularmente:

```bash
./venv/bin/python prod/stock-sync/worker.py list-review
```

Corrija el producto o SKU indicado antes de reintentar su `JOB_ID`. Un trabajo en `needs_review` no se aplica automaticamente. Los mensajes de salida y `sync_logs` de SQLite son el registro operativo; evite imprimir o copiar valores de tokens.

Ejecute la revision diaria primero sin cambios y revise el JSON `planned_updates`:

```bash
./venv/bin/python prod/stock-sync/worker.py daily --dry-run
```

Solo despues de aprobacion explicita puede ejecutar la version aplicada:

```bash
./venv/bin/python prod/stock-sync/worker.py daily
```

## Respaldo Y Rollback

Detenga primero el receiver y el worker. Antes de cualquier migracion o ejecucion aplicada, haga un respaldo SQLite consistente fuera del repositorio:

```bash
backup_dir=/var/backups/zipp-stock-sync
backup="$backup_dir/stock_sync.db.$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
test -f "$STOCK_SYNC_DATABASE"
./venv/bin/python -c 'import sqlite3, sys; source, destination = sys.argv[1:]; src = sqlite3.connect(source); dst = sqlite3.connect(destination); src.backup(dst); dst.close(); src.close()' "$STOCK_SYNC_DATABASE" "$backup"
```

Conserve la ruta del respaldo y confirme que existe antes de continuar. Para volver atras la cola local, conserve la base actual como evidencia del incidente y reemplacela solo cuando todos los procesos del nuevo servicio esten detenidos:

```bash
test -f "$backup"
incident_db="${STOCK_SYNC_DATABASE}.incident-$(date -u +%Y%m%dT%H%M%SZ)"
mv "$STOCK_SYNC_DATABASE" "$incident_db"
rm -f "${STOCK_SYNC_DATABASE}-wal" "${STOCK_SYNC_DATABASE}-shm"
cp "$backup" "$STOCK_SYNC_DATABASE"
```

No elimine `$incident_db`. No reinicie el worker aplicado hasta revisar el incidente. Restaurar SQLite no deshace una orden Shopify ni una cantidad de Mercado Libre que ya cambio; esos efectos requieren una correccion separada y revisada en cada plataforma.

## Control De Despliegue En Vivo (Pendiente)

El siguiente despliegue de produccion **no** fue realizado por este cambio.

1. Detenga los procesos del nuevo servicio, haga el respaldo SQLite y ejecute `migrate`.
2. Con los procesadores anteriores aun activos, encole solo una orden pagada y conocida de Mercado Libre en la nueva cola y ejecute `once --dry-run`.
3. Verifique manualmente los IDs de variante Shopify propuestos, cantidades, precios unitarios, moneda, tags `mercadolibre`/`meli-order-<id>` y el comportamiento de un decremento de inventario.
4. Detengase y obtenga confirmacion explicita del usuario antes de cualquier importacion aplicada. No ejecute `once`, `daily` ni `run` en modo aplicado antes de esa confirmacion.
5. Tras la confirmacion, detenga los dos procesadores anteriores, ejecute exactamente un `once` revisado y verifique una orden Shopify, un decremento de inventario y el enlace local de la orden.
6. Ejecute una reconciliacion de SKU revisada, verifique ambas plataformas y los logs, y solo entonces habilite `run` y la tarea diaria aplicada.

Si aparece un problema, detenga el worker y conserve la base de datos y los logs. No elimine ordenes importadas ni revierta automaticamente cambios de inventario confirmados.
