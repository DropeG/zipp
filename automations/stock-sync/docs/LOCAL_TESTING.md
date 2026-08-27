# Pruebas Locales: Stock Sync

Manual humano para probar el sincronizador Shopify <-> Mercado Libre desde un computador local.

La meta es imitar produccion lo suficiente para confirmar que:

- Shopify o Mercado Libre pueden enviar webhooks al computador local.
- El catcher recibe esos webhooks.
- SQLite guarda el evento.
- El sincronizador crea o procesa tareas.
- El dry-run entiende que stock cambiaria.
- El humano decide si quiere aplicar o solo limpiar la prueba.

## Idea Simple

Tu computador no es publico en internet. Shopify y Mercado Libre no pueden enviar webhooks directo a `localhost:3000`.

`cloudflared` crea una URL publica temporal que apunta a tu computador:

```text
Shopify / Mercado Libre
        |
        v
https://xxxxx.trycloudflare.com
        |
        v
http://localhost:3000
        |
        v
webhook_catcher.js
        |
        v
data/stock_sync.db
```

Importante: `http://localhost:3000` no es una pagina visual. Si lo abres en el navegador puede mostrar `Not found`, y eso esta bien. Este servicio es una puerta para recibir webhooks `POST`.

## Antes De Empezar

Ejecutar comandos desde la raiz del repo.

Debe existir:

- `.env` con credenciales reales.
- `meli_tokens.json` generado con la cuenta correcta de Mercado Libre.
- `venv` con dependencias instaladas.
- Node.js 22 o superior.
- `cloudflared` instalado.
- Acceso a Shopify Admin.
- Acceso a la app de Mercado Libre usada por Zipp.

Instalacion base si todavia falta algo:

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

## Prueba Guiada: Shopify -> Mercado Libre

Esta es la prueba recomendada para confirmar que el webhook local funciona.

En esta prueba se crea una orden real o manual de Shopify, pero primero solo se llega hasta dry-run. Eso permite confirmar el flujo sin tocar stock en Mercado Libre.

### 1. Levantar El Catcher

En una terminal:

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js
```

Debe quedar abierto y mostrar algo parecido a:

```text
Escuchando en http://localhost:3000
Guardando datos en: .../data/stock_sync.db
```

Si aparece un warning de SQLite experimental en Node, no bloquea esta prueba.

### 2. Abrir El Tunel

En otra terminal:

```bash
cloudflared tunnel --url http://localhost:3000
```

Cloudflared mostrara una URL publica temporal:

```text
https://nombre-temporal.trycloudflare.com
```

Copiar esa URL. Mientras el comando siga corriendo, Shopify podra llegar a tu computador por esa direccion.

### 3. Probar Que El Tunel Llega Al Catcher

Opcional, pero util para entenderlo:

```bash
curl -i https://nombre-temporal.trycloudflare.com/
```

Es normal recibir:

```text
HTTP/2 404
Not found
```

Eso significa que Cloudflare si llego al catcher, pero la ruta `/` no existe. Los webhooks reales usan rutas especificas y metodo `POST`.

### 4. Crear Webhook Temporal En Shopify

En Shopify Admin:

```text
Settings / Configuracion
Notifications / Notificaciones
Webhooks
Create webhook / Crear webhook
```

Configurar:

```text
Event / Evento: Order creation / orders/create
Format / Formato: JSON
URL: https://nombre-temporal.trycloudflare.com/webhooks/shopify/orders-create
```

Guardar el webhook.

No usar una URL antigua. Cada tunnel nuevo crea una URL nueva.

### 5. Crear Una Orden Manual De Prueba

En Shopify Admin:

```text
Orders / Pedidos
Create order / Crear pedido
```

Elegir un producto que:

- exista en Shopify
- exista en Mercado Libre
- tenga el mismo SKU en ambos lados
- tenga stock suficiente

Antes de crear la orden, anotar:

```text
SKU:
Stock Shopify antes:
Stock Mercado Libre antes:
```

Crear la orden con:

```text
Cantidad: 1
Cliente: prueba o cliente interno
Nota: pedido de prueba
Pago: marcar como pagado, si Shopify lo pide para crear la orden final
```

La orden debe quedar como orden real, no solo como borrador. En nuestra prueba validada fue una orden tipo `#1283`, pagada, con SKU `PLA-ZIP-CI-95`.

### 6. Confirmar Que Llego Al Catcher

Mirar la terminal donde corre `webhook_catcher.js`.

Un webhook exitoso de Shopify se ve asi:

```text
Raw event Shopify guardado
Webhook ID: ...
Topic: orders/create
Shop: ...
Order ID: ...
Order name: #1283
Stock task creada: PLA-ZIP-CI-95 -> pending
```

Si dice `Stock task ya existia, no se duplico`, significa que la misma orden ya estaba registrada antes. Eso tambien confirma que llego, pero no sirve como prueba nueva del flujo completo.

### 7. Confirmar En SQLite

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

```text
source = shopify
topic = orders/create
order_name = #NUMERO_DE_ORDEN
```

Ver tareas recientes:

```sql
SELECT task_id, source, order_id, order_name, sku, quantity_sold, status, human_note, updated_at
FROM stock_tasks
ORDER BY updated_at DESC
LIMIT 20;
```

Resultado esperado:

```text
source = shopify
sku = SKU_DE_LA_ORDEN
quantity_sold = 1
status = pending
```

### 8. Ejecutar Dry-Run

Dry-run no toca stock real. Solo revisa que el sistema sabe que haria.

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10
```

Un resultado bueno se ve asi:

```text
Modo: dry-run (no actualiza Mercado Libre)
Tareas pending encontradas: 1
Shopify location activa: ...
Publicaciones Meli cargadas para busqueda SKU: ...

Procesando PLA-ZIP-CI-95 (#1283)
  Shopify stock actual: 6
  Meli item: MLC4223500744 (Placa De Metal Adhesiva Redonda)
  Meli stock actual: 7
  -> ready_to_apply: Meli quedaria en 6

Dry-run terminado.
```

Como leerlo:

- `Shopify stock actual: 6`: Shopify ya bajo el stock por la orden.
- `Meli stock actual: 7`: Mercado Libre todavia esta en el stock anterior.
- `Meli quedaria en 6`: el sincronizador quiere igualar Meli con Shopify.
- `ready_to_apply`: la tarea esta lista para aplicar si el humano decide tocar Meli.

Hasta aqui Mercado Libre no fue modificado.

### 9. Si Solo Querias Confirmar Que Funcionaba

Si el objetivo era confirmar webhook + dry-run, puedes parar aqui.

No ejecutes:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 1
```

Ese comando si toca stock real en Mercado Libre.

Para dejar la prueba cerrada:

1. Cancelar o reembolsar la orden de prueba en Shopify.
2. Marcar la opcion para devolver el articulo al inventario.
3. Revisar que el stock de Shopify volvio al numero anterior.
4. Eliminar o desactivar el webhook temporal de Shopify.
5. Detener `cloudflared`.
6. Detener `webhook_catcher.js`.
7. Neutralizar la tarea local si quedo en `ready_to_apply`.

Para neutralizar la tarea local, usar el `order_id` real de la orden de prueba:

```sql
UPDATE stock_tasks
SET status = 'needs_review',
    human_note = 'Prueba local cancelada/revertida manualmente. No aplicar al stock de Mercado Libre.',
    updated_at = datetime('now')
WHERE order_id = 'ORDER_ID_DE_PRUEBA'
  AND status = 'ready_to_apply';
```

Confirmar:

```sql
SELECT task_id, order_name, sku, quantity_sold, status, human_note
FROM stock_tasks
WHERE order_id = 'ORDER_ID_DE_PRUEBA';
```

Resultado esperado:

```text
status = needs_review
human_note = Prueba local cancelada/revertida manualmente...
```

## Aplicar Un Cambio Real Controlado

Usar esta seccion solo si el humano decide probar el cambio real de stock en Mercado Libre.

Antes de aplicar:

- Revisar el dry-run.
- Confirmar SKU.
- Confirmar stock actual en Shopify.
- Confirmar item correcto en Mercado Libre.
- Usar `--limit 1`.

Comando:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 1
```

Despues del apply, revisar:

```sql
SELECT task_id, source, order_id, order_name, sku, quantity_sold, status, human_note, updated_at
FROM stock_tasks
ORDER BY updated_at DESC
LIMIT 20;
```

Resultado esperado:

```text
status = synced
```

Tambien revisar manualmente el stock final en Shopify y Mercado Libre.

## Prueba Mercado Libre -> Shopify

El flujo Meli -> Shopify usa el mismo tunnel, pero el webhook temporal debe apuntar a:

```text
https://nombre-temporal.trycloudflare.com/webhooks/meli/orders
```

Despues de recibir una notificacion Meli, correr:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
```

Para una orden puntual:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id ORDER_ID
```

Aplicar, solo si corresponde:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 1
```

## Como Saber Si La Prueba Fue Exitosa

Para una prueba Shopify -> Mercado Libre sin apply, basta con:

- [ ] El catcher estaba corriendo.
- [ ] El tunnel `cloudflared` estaba activo.
- [ ] Shopify tenia un webhook temporal apuntando al tunnel actual.
- [ ] Se creo una orden real/manual de prueba.
- [ ] El catcher imprimio `Raw event Shopify guardado`.
- [ ] El catcher mostro el numero de orden real, no solo `#9999`.
- [ ] SQLite mostro el evento en `raw_events`.
- [ ] SQLite mostro una tarea nueva en `stock_tasks`.
- [ ] El dry-run encontro el SKU en Mercado Libre.
- [ ] El dry-run dejo la tarea en `ready_to_apply`.
- [ ] No se ejecuto `--apply` si la prueba era solo de confirmacion.
- [ ] La orden de prueba fue cancelada/reembolsada con restock en Shopify.
- [ ] El webhook temporal fue eliminado o desactivado.
- [ ] El tunnel y el catcher fueron detenidos.
- [ ] La tarea local quedo neutralizada si estaba `ready_to_apply`.

## Limpieza Rapida

Al terminar una prueba local:

1. Borrar o desactivar webhooks temporales que apunten a `trycloudflare.com`.
2. Cerrar `cloudflared` con `Ctrl+C`.
3. Cerrar `webhook_catcher.js` con `Ctrl+C`.
4. Cancelar/reembolsar la orden de prueba si no era una venta real.
5. Confirmar que Shopify devolvio el stock.
6. No borrar `data/stock_sync.db` si se quiere conservar evidencia de la prueba.

## Recordatorio Mental

```text
Webhook recibido = conectividad confirmada
Tarea pending = catcher funciono
Dry-run ready_to_apply = sincronizador entendio que haria
Apply synced = stock real modificado
```

No hace falta llegar a `apply` para demostrar que el webhook local funciona.
