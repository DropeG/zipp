# Stock Sync De Produccion

Este servicio recibe webhooks, los guarda en SQLite y un solo worker los procesa. Una venta pagada de Mercado Libre puede crear una orden pagada y etiquetada en Shopify; una orden de Shopify o la tarea diaria puede actualizar la cantidad de Mercado Libre. El receiver solo encola trabajo: no llama a Shopify ni a Mercado Libre.

Todos los comandos de este documento se ejecutan desde la raiz del repositorio. Las imagenes fijan Python 3.14.6 y Node.js 24.19.0; el host solo necesita Docker Engine y Docker Compose v2.

## Instalacion Con Docker Compose

```bash
cp prod/stock-sync/.env.example prod/stock-sync/.env
chmod 600 prod/stock-sync/.env
```

Complete `prod/stock-sync/.env`. Los tokens iniciales de Mercado Libre se obtienen con el flujo OAuth temporal documentado abajo; no se copian manualmente al archivo.

Defina este helper en cada terminal operativa; `--env-file` carga la configuracion Compose sin imprimirla:

```bash
dc() { docker compose --env-file prod/stock-sync/.env "$@"; }
```

Construya las imagenes y ejecute las pruebas aisladas antes de iniciar servicios:

```bash
dc build receiver worker python-tests node-tests
dc run --rm --no-deps node-tests
dc run --rm --no-deps python-tests
```

Las pruebas no tienen red. El receiver y el worker corren como UID/GID `10001`, con filesystem raiz de solo lectura y capacidades Linux eliminadas. `data-init` prepara los directorios persistentes. No suba `.env`, tokens, certificados, bases ni respaldos.

## Credenciales Y Variables De Entorno

| Variable | Requerida | Uso |
| --- | --- | --- |
| `SHOPIFY_SHOP_URL` | Si | URL `https://<tienda>.myshopify.com` de la tienda. |
| `SHOPIFY_ACCESS_TOKEN` | Si | Token de la app Admin de Shopify. |
| `SHOPIFY_WEBHOOK_SECRET` | Si | Secreto para validar `orders/create` de Shopify. |
| `MELI_APP_ID` | Si | Client ID de la aplicacion de Mercado Libre. |
| `MELI_CLIENT_SECRET` | Si | Client secret de la aplicacion de Mercado Libre. |
| `MELI_EXPECTED_SELLER_ID` | Si | ID exacto del vendedor autorizado. El worker rechaza ordenes, listings y tokens de otro vendedor. |
| `MELI_WEBHOOK_TOKEN` | Si | Secreto que Nginx inyecta como `X-Webhook-Token` solo despues de aceptar una IP de notificacion de Mercado Libre. Mercado Libre no envia este encabezado. |
| `MELI_TOKENS_FILE` | No; tiene ruta por defecto | Ruta dentro del contenedor. Por defecto: `/data/meli_tokens.json`. |
| `MELI_IMPORT_CUTOVER_AT` | Recomendado al migrar | Timestamp ISO-8601 con zona horaria. Una orden Mercado Libre anterior se reconoce pero no se importa si aun no tiene enlace local; evita descontar otra vez ventas procesadas por el sistema anterior. |
| `STOCK_SYNC_DATABASE` | No; tiene ruta por defecto | Ruta SQLite dentro del contenedor. Por defecto: `/data/stock_sync.db`. |
| `SHOPIFY_API_VERSION` | No; tiene valor por defecto | Version Admin GraphQL. Por defecto: `2026-07`. |
| `MAX_WEBHOOK_BYTES` | No; tiene valor por defecto | Tamano maximo aceptado por webhook. Por defecto: `1048576`. |
| `HOST` | No; Compose lo fija | `0.0.0.0` dentro de la red Docker privada; el puerto no se publica al host. |
| `PORT` | No; tiene valor por defecto | Puerto HTTP del receiver. Por defecto: `3000`. |
| `STOCK_SYNC_PUBLIC_HOST` | Para el perfil `edge` | Hostname publico TLS usado por Nginx. |

Tambien es obligatorio disponer de un archivo de tokens valido de Mercado Libre en `MELI_TOKENS_FILE`. Debe contener `access_token`, `refresh_token` y `expires_at` (timestamp Unix). El servicio OAuth temporal lo crea con permisos `0600`; despues el worker refresca el token y reemplaza ese archivo de forma atomica. No escriba ni pegue esos tokens en `.env`.

La app de Shopify debe conceder exactamente estos scopes requeridos por el servicio: `read_products`, `read_inventory`, `read_orders`, `write_orders` y `write_draft_orders`. El acceso de Mercado Libre debe pertenecer al vendedor configurado y permitir leer el usuario, ordenes e items/listings, y actualizar la cantidad disponible de items. No hay un nombre de scope de Mercado Libre configurado en el codigo.

## Autorizacion Inicial De Mercado Libre

En la aplicacion de Mercado Libre registre exactamente esta redirect URL, sustituyendo el host por `STOCK_SYNC_PUBLIC_HOST`:

```text
https://sync.zipp.cl/oauth/meli/callback
```

El DNS debe apuntar al servidor y Nginx debe tener un certificado TLS valido para ese host. Con los puertos 80/443 libres, emita inicialmente el certificado mediante el contenedor Certbot fijado en Compose:

```bash
dc --profile tls run --rm --service-ports certbot
```

Complete primero `MELI_APP_ID` y `MELI_CLIENT_SECRET` en `.env`. Si ya conoce el ID numerico del vendedor, configure tambien `MELI_EXPECTED_SELLER_ID`; si no lo conoce, dejelo vacio durante este bootstrap. Luego genere una solicitud de un solo uso:

```bash
dc run --rm oauth start
```

El comando imprime una URL de Mercado Libre y guarda localmente un `state` y verificador PKCE con una vigencia de 15 minutos. No comparta esa URL. Inicie el callback temporal y Nginx:

```bash
dc --profile edge --profile oauth up -d receiver oauth nginx
```

Abra la URL impresa, inicie sesion como la cuenta vendedora principal y autorice la aplicacion. No use una cuenta de operador o colaborador. El callback comprueba el `state`, canjea el codigo y, si `MELI_EXPECTED_SELLER_ID` ya estaba configurado, rechaza otra cuenta. Si todo sale bien, el navegador muestra `Autorizacion completada` junto al ID numerico, los tokens quedan en `prod/stock-sync/data/meli_tokens.json` y el servicio `oauth` se apaga. Si el ID estaba vacio, copielo a `MELI_EXPECTED_SELLER_ID` antes de ejecutar cualquier comando del worker. Confirme el resultado sin mostrar secretos:

```bash
dc ps -a oauth
dc run --rm --no-deps --entrypoint python worker - <<'PY'
import json
from pathlib import Path

path = Path("/data/meli_tokens.json")
payload = json.loads(path.read_text())
required = ("access_token", "refresh_token", "expires_at")
if not all(payload.get(key) for key in required):
    raise SystemExit("El archivo de tokens no esta completo")
print("Tokens OAuth de Mercado Libre guardados correctamente.")
PY
```

Este flujo no inicia el perfil `live` ni ejecuta cambios de stock. Durante la operacion normal no se necesita el contenedor `oauth`; si desea retirarlo de la lista de contenedores detenidos, use `dc rm -f oauth`.

## Entrada De Webhooks

El receiver no se expone directamente al host ni a Internet: solo pertenece a la red Docker interna `receiver`. Nginx es la unica entrada publica. Antes de activar el perfil `edge`, emita el certificado Let's Encrypt con el perfil `tls`; se guarda bajo `prod/stock-sync/certs/` y no se versiona. El bloque de Mercado Libre acepta `POST /webhooks/meli` solo desde la lista oficial de IPs, reemplaza cualquier encabezado entrante e inyecta `X-Webhook-Token` con el mismo secreto configurado en `MELI_WEBHOOK_TOKEN`. Mercado Libre no envia ese encabezado.

La lista incluida se copio de la documentacion oficial de [notificaciones de Mercado Libre Chile](https://developers.mercadolibre.cl/es_ar/publica-productos/productos-recibe-notificaciones) el 2026-09-07. Las IPs pueden cambiar: el operador debe revisar esa pagina y actualizar el ejemplo desplegado antes de cada rollout. Configure la URL publica de callback de Mercado Libre hacia la ruta Nginx `POST /webhooks/meli`, no hacia `127.0.0.1`.

Shopify puede usar su propia ruta proxied `POST /webhooks/shopify/orders-create`; no necesita el secreto interno de Mercado Libre y el receiver conserva la verificacion HMAC de Shopify. El endpoint local `GET /health` responde `ok`.

## Comandos

Los ejemplos suponen que la funcion `dc` anterior existe. Estos son los nombres reales de `worker.py`; no hay comandos `--apply`, `--order-id` ni `--limit`.

| Objetivo | Comando | Efecto |
| --- | --- | --- |
| Crear o actualizar el esquema local | `dc run --rm worker migrate` | Solo SQLite; no llama APIs. |
| Crear enlace OAuth inicial | `dc run --rm oauth start` | Genera `state` y PKCE; no modifica stock. |
| Esperar callback OAuth | `dc --profile edge --profile oauth up -d receiver oauth nginx` | Guarda tokens; no inicia el worker. |
| Emitir/renovar certificado | `dc --profile tls run --rm --service-ports certbot` | Usa HTTP-01 en puerto 80; no inicia la aplicacion. |
| Ejecutar pruebas Python | `dc run --rm --no-deps python-tests` | Pruebas aisladas sin red. |
| Ejecutar pruebas Node | `dc run --rm --no-deps node-tests` | Receiver aislado sin red. |
| Iniciar receiver privado | `dc up -d receiver` | Solo en la red Docker interna; encola webhooks. |
| Iniciar entrada TLS | `dc --profile edge up -d receiver nginx` | Publica solo Nginx en `80/443`. |
| Inspeccionar un trabajo | `dc run --rm worker once --dry-run` | Lee APIs, sin crear ordenes ni actualizar stock. |
| Aplicar un trabajo | `dc run --rm worker once` | Puede crear una orden Shopify o actualizar Mercado Libre. Requiere confirmacion. |
| Worker continuo | `dc --profile live up -d worker` | Aplica cada trabajo disponible; el perfil `live` no se inicia por defecto. |
| Revision diaria sin cambios | `dc run --rm worker daily --dry-run` | Muestra importaciones y cantidades propuestas sin cambiarlas. |
| Revision diaria aplicada | `dc run --rm worker daily` | Puede importar ordenes y actualizar cantidades. Requiere confirmacion. |
| Listar trabajos en revision | `dc run --rm worker list-review` | Muestra JSON de `needs_review` sin requerir credenciales API. |
| Reintentar un trabajo revisado | `dc run --rm worker retry JOB_ID` | Devuelve ese trabajo a `pending`; no llama APIs. |

`once --dry-run` toma el primer trabajo elegible. El CLI no permite elegir una orden Mercado Libre por ID. Para una prueba controlada, asegure que la nueva cola contiene solo el webhook de la orden revisada antes de ejecutar ese comando. Si hay mas trabajos pendientes, no ejecute `once` hasta aislar la cola.

## Operacion Normal

Inicie el receiver y el worker como procesos separados. Nginx entrega los dos proveedores al receiver local; el worker usa un bloqueo SQLite, por lo que solo puede procesar un trabajo o una revision diaria a la vez. Revise regularmente:

```bash
dc run --rm worker list-review
```

Corrija el producto o SKU indicado antes de reintentar su `JOB_ID`. Un trabajo en `needs_review` no se aplica automaticamente. Los mensajes de salida y `sync_logs` de SQLite son el registro operativo; evite imprimir o copiar valores de tokens.

Las notificaciones de Mercado Libre se deduplican por su `_id` de entrega. Cada notificacion posterior puede volver a consultar la misma orden; el enlace local de orden evita importar otra venta. Sin `_id`, se acepta una nueva consulta por entrega.

Antes de enviar `orderCreate`, el worker guarda un registro en `order_creates`. Si Shopify pudo aceptar la orden pero se pierde la respuesta, una busqueda sin resultado no autoriza otra creacion. El trabajo requiere conciliacion humana y `retry JOB_ID` solo vuelve a buscar la orden existente. Verifique en Shopify la etiqueta `meli-order-ID` y el identificador de origen; cuando esa orden sea visible, el reintento guarda su enlace y resuelve el borrador incluyendo el ID Shopify. No borre el registro de intento para forzar otra orden: si no se puede probar el resultado, mantenga el trabajo en revision. Una respuesta explicita de validacion con `order: null` y `userErrors` permite corregir el problema y reintentar la creacion.

Los problemas de una orden real se resuelven por separado: reparar un SKU conserva cualquier otro problema pendiente en esa orden. Un stock Shopify negativo se copia a Mercado Libre como cero y mantiene una revision de faltante en la orden real hasta corregirlo. Los borradores usan primero su enlace SQLite y conservan sus notas al resolverse.

Ejecute la revision diaria primero sin cambios y revise el JSON `planned_updates`:

```bash
dc run --rm worker daily --dry-run
```

Solo despues de aprobacion explicita puede ejecutar la version aplicada:

```bash
dc run --rm worker daily
```

## Respaldo Y Rollback

Detenga primero el receiver y el worker. Antes de cualquier migracion o ejecucion aplicada, haga un respaldo SQLite consistente fuera del repositorio:

```bash
stock_db_path="${STOCK_SYNC_DATABASE:-/data/stock_sync.db}"
backup_dir=/backups
backup="$backup_dir/stock_sync.db.$(date -u +%Y%m%dT%H%M%SZ)"
dc run --rm --no-deps --entrypoint python worker - "$stock_db_path" "$backup" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1:]
with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
with sqlite3.connect(destination) as database:
    if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("Backup integrity check failed")
print(destination)
PY
```

Conserve la ruta del respaldo y confirme que existe antes de continuar. Para volver atras la cola local, primero cree una copia SQLite consistente de la base actual como evidencia del incidente. Esto incluye el estado que este en WAL; no mueva solo el archivo principal ni elimine archivos WAL.

```bash
stock_db_path="${STOCK_SYNC_DATABASE:-/data/stock_sync.db}"
backup_dir=/backups
backup=REEMPLACE_CON_LA_RUTA_EN_/backups
incident_snapshot="$backup_dir/stock_sync.db.incident-$(date -u +%Y%m%dT%H%M%SZ)"
dc run --rm --no-deps --entrypoint python worker - "$stock_db_path" "$incident_snapshot" "$backup" <<'PY'
import sqlite3
import sys
from pathlib import Path

current_database, incident_snapshot, previous_backup = sys.argv[1:]
for required in (current_database, previous_backup):
    if not Path(required).is_file():
        raise RuntimeError(f"Missing required database: {required}")
if Path(incident_snapshot).exists():
    raise RuntimeError(f"Incident snapshot already exists: {incident_snapshot}")

with sqlite3.connect(current_database) as source, sqlite3.connect(incident_snapshot) as destination:
    source.backup(destination)

snapshot_uri = f"{Path(incident_snapshot).resolve().as_uri()}?mode=ro"
with sqlite3.connect(snapshot_uri, uri=True) as snapshot:
    if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("Incident snapshot integrity check failed")

with sqlite3.connect(previous_backup) as source, sqlite3.connect(current_database) as destination:
    source.backup(destination)
PY
```

No elimine `$incident_snapshot`. No reinicie el worker aplicado hasta revisar el incidente. Restaurar SQLite no deshace una orden Shopify ni una cantidad de Mercado Libre que ya cambio; esos efectos requieren una correccion separada y revisada en cada plataforma.

## Control De Despliegue En Vivo (Pendiente)

El siguiente despliegue de produccion **no** fue realizado por este cambio.

1. Detenga receiver y worker nuevos. Para esta primera prueba use una base nueva y aislada, no la base de los procesadores anteriores. Elija una orden pagada conocida con un solo SKU distinto y defina su ID:

   ```bash
   stock_db_path=/data/first-rollout.db
   host_stock_db_path="$PWD/prod/stock-sync/data/first-rollout.db"
   known_order_id=REEMPLACE_CON_LA_ORDEN_REVISADA
   if test -e "$host_stock_db_path" || test -e "${host_stock_db_path}-wal" || test -e "${host_stock_db_path}-shm"; then
     echo "La base de primera prueba no esta vacia; abortar."
     exit 1
   fi
   export STOCK_SYNC_DATABASE="$stock_db_path"
   dc run --rm worker migrate
   ```

2. Inicie solo el receiver privado y envie exactamente un aviso determinista desde el propio contenedor; el encabezado simula el que inyectara Nginx, no un encabezado de Mercado Libre:

   ```bash
   dc up -d receiver
   ```

   ```bash
   dc exec -T -e KNOWN_ORDER_ID="$known_order_id" receiver node - <<'JS'
   const body = JSON.stringify({
     _id: `first-rollout-${process.env.KNOWN_ORDER_ID}`,
     resource: `/orders/${process.env.KNOWN_ORDER_ID}`,
     topic: "orders_v2",
   });
   fetch("http://127.0.0.1:3000/webhooks/meli", {
     method: "POST",
     headers: {
       "Content-Type": "application/json",
       "X-Webhook-Token": process.env.MELI_WEBHOOK_TOKEN,
     },
     body,
   }).then((response) => {
     if (!response.ok) throw new Error(`Webhook failed: HTTP ${response.status}`);
     console.log("Webhook accepted.");
   }).catch((error) => { console.error(error.message); process.exit(1); });
   JS
   dc stop receiver
   ```

   No inicie el worker continuo aun.

3. Verifique en modo de solo lectura que existe exactamente un trabajo pendiente y que corresponde a esa orden. Si el comando falla, aborte la prueba y no ejecute `once`:

   ```bash
   dc run --rm --no-deps --entrypoint python worker - "$stock_db_path" "$known_order_id" <<'PY'
   import sqlite3
   import sys
   from pathlib import Path

   database_path, order_id = sys.argv[1:]
   uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
   with sqlite3.connect(uri, uri=True) as database:
       rows = database.execute(
           "SELECT job_type, source_key, resource_key, status FROM jobs ORDER BY id"
       ).fetchall()
   expected = [(
       "import_meli_order",
       f"meli-notice:{order_id}:first-rollout-{order_id}",
       f"order:{order_id}",
       "pending",
   )]
   if rows != expected:
       raise SystemExit(f"Expected one pending import job, found: {rows!r}")
   print(rows[0])
   PY
   ```

4. Con los procesadores anteriores aun activos, ejecute `dc run --rm worker once --dry-run`. Verifique manualmente los IDs de variante Shopify propuestos, cantidades, precios unitarios, moneda, tags `mercadolibre`/`meli-order-<id>` y el comportamiento de un decremento de inventario. Repita el comando de solo lectura anterior: el trabajo debe haber vuelto a `pending`.
5. Detengase y obtenga confirmacion explicita del usuario antes de aplicar la importacion. Tras esa confirmacion, detenga los dos procesadores anteriores antes de ejecutar el primer comando aplicado:

   ```bash
   dc run --rm worker once
   ```

   El resultado incluye `shopify_order_id`. En Shopify, verifique manualmente que existe exactamente una orden con el tag `meli-order-$known_order_id`, que tiene los valores revisados y que el inventario se desconto una vez. Defina el ID recibido y confirme el enlace local en modo de solo lectura:

   ```bash
   shopify_order_id=REEMPLACE_CON_EL_ID_DEVUELTO
   dc run --rm --no-deps --entrypoint python worker - "$stock_db_path" "$known_order_id" "$shopify_order_id" <<'PY'
   import sqlite3
   import sys
   from pathlib import Path

   database_path, order_id, expected_shopify_order_id = sys.argv[1:]
   uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
   with sqlite3.connect(uri, uri=True) as database:
       row = database.execute(
           "SELECT shopify_order_id FROM order_links WHERE meli_order_id = ?",
           (order_id,),
       ).fetchone()
   if row != (expected_shopify_order_id,):
       raise SystemExit(f"Expected linked Shopify order {expected_shopify_order_id!r}, found: {row!r}")
   print(row[0])
   PY
   ```

6. Antes de procesar la reconciliacion, verifique que existe exactamente un trabajo elegible: `reconcile_sku`, `pending`, para el SKU revisado y para ese `shopify_order_id`. El comando tambien aborta si queda una importacion elegible. No ejecute `once` si falla:

   ```bash
   reviewed_sku=REEMPLACE_CON_EL_SKU_REVISADO
   dc run --rm --no-deps --entrypoint python worker - "$stock_db_path" "$reviewed_sku" "$shopify_order_id" <<'PY'
   import json
   import sqlite3
   import sys
   from datetime import datetime, timezone
   from pathlib import Path

   database_path, expected_sku, expected_shopify_order_id = sys.argv[1:]
   uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
   with sqlite3.connect(uri, uri=True) as database:
       rows = database.execute(
           "SELECT job_type, status, available_at, lease_until, payload, resource_key FROM jobs ORDER BY id"
       ).fetchall()

   now = datetime.now(timezone.utc)
   def timestamp(value):
       return datetime.fromisoformat(value).astimezone(timezone.utc)
   active_resources = {
       row[5] for row in rows
       if row[1] == "processing" and row[3] is not None and timestamp(row[3]) > now
   }
   def eligible(row):
       job_type, status, available_at, lease_until, payload, resource_key = row
       if resource_key in active_resources:
           return False
       if status in {"pending", "retry_wait"}:
           return timestamp(available_at) <= now
       return status == "processing" and lease_until is not None and timestamp(lease_until) <= now

   eligible_jobs = [row for row in rows if eligible(row)]
   if any(row[0] == "import_meli_order" for row in eligible_jobs):
       raise SystemExit(f"Eligible import job remains: {eligible_jobs!r}")
   if len(eligible_jobs) != 1:
       raise SystemExit(f"Expected exactly one eligible job, found: {eligible_jobs!r}")
   job_type, status, available_at, lease_until, payload, resource_key = eligible_jobs[0]
   if (job_type, status) != ("reconcile_sku", "pending"):
       raise SystemExit(f"Expected pending reconcile_sku, found: {eligible_jobs[0]!r}")
   payload = json.loads(payload)
   if payload != {"sku": expected_sku, "shopify_order_id": expected_shopify_order_id}:
       raise SystemExit(f"Unexpected reconciliation payload: {payload!r}")
   print({"job_type": job_type, "status": status, "payload": payload})
   PY
   ```

   Inspeccione ese unico trabajo sin cambios y verifique las cantidades propuestas:

   ```bash
   dc run --rm worker once --dry-run
   ```

   Obtenga confirmacion explicita antes de aplicar esa reconciliacion y solo entonces ejecute:

   ```bash
   dc run --rm worker once
   ```

7. Solo despues de verificar la reconciliacion de SKU, ambas plataformas y los logs, habilite la entrada publica y el worker continuo:

   ```bash
   dc --profile edge up -d receiver nginx
   dc --profile live up -d worker
   ```

   Mantenga la tarea diaria en modo manual hasta revisar y aprobar su primer `daily --dry-run`.

Si aparece un problema, detenga el worker y conserve la base de datos y los logs. No elimine ordenes importadas ni revierta automaticamente cambios de inventario confirmados.
