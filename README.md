# Zipp Shopify / Mercado Libre Sync

Manual de traspaso para dejar andando el sincronizador de Zipp en servidor.

## Que Hace

Este repositorio contiene las herramientas que armamos para conectar Shopify con Mercado Libre Chile:

- Publicar productos de Shopify en Mercado Libre, usando datos e imagenes de Shopify.
- Mantener un registro local de productos ya publicados en `sync_mappings.json`.
- Recibir webhooks de ventas de Shopify y Mercado Libre.
- Crear tareas idempotentes de ajuste de stock en SQLite.
- Procesar esas tareas primero en dry-run y luego en apply.
- Evitar tocar stock cuando hay ambiguedad de SKU, SKU duplicado o permisos insuficientes.

Regla principal: Shopify es la fuente digital de verdad para productos y stock. El SKU es la identidad compartida entre Shopify y Mercado Libre.

## Estado Actual

Validado localmente:

- Shopify -> Mercado Libre stock sync funciona end-to-end.
- Mercado Libre -> Shopify stock sync funciona end-to-end.
- El flujo es idempotente: reprocesar la misma orden no descuenta dos veces.
- El apply re-lee stock fresco antes de modificar inventario.

Contexto historico y pruebas reales: ver `STOCK_SYNC_CONTEXT.md`.

## Estructura Importante

```text
sync_products.py                         Publica/sincroniza productos Shopify -> Meli.
publish_payloads.py                      Publica payloads preparados a Mercado Libre.
shopify_client.py                        Cliente simple Shopify.
meli_client.py                           Cliente Mercado Libre con OAuth y refresh token.
ai_client.py                             Optimizacion y extraccion de atributos con Gemini.
scripts/shopify_webhook_catcher.js       Servidor HTTP para webhooks Shopify y Meli.
scripts/process_shopify_to_meli_stock.py Procesa ventas Shopify -> actualiza stock Meli.
scripts/process_meli_to_shopify_stock.py Procesa ventas Meli -> actualiza stock Shopify.
data/stock_sync.db                       Base SQLite local/produccion, no versionada.
sync_mappings.json                       Mapeo Shopify product ID -> Mercado Libre item ID.
.env.example                             Variables necesarias sin secretos reales.
```

## Requisitos

- Python 3.11 o superior.
- Node.js 22 o superior. El catcher usa `node:sqlite`.
- Credenciales Shopify Admin API.
- App de Mercado Libre con permisos de publicacion y ventas/envios.
- Dominio publico o tunnel estable para recibir webhooks.

## Instalacion En Servidor

```bash
git clone https://github.com/DropeG/zipp.git
cd zipp

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
```

Editar `.env` con los valores reales:

```text
SHOPIFY_SHOP_URL=...
SHOPIFY_ACCESS_TOKEN=...
SHOPIFY_API_VERSION=2024-04
MELI_APP_ID=...
MELI_CLIENT_SECRET=...
MELI_REDIRECT_URI=https://localhost
GEMINI_API_KEY=...
```

No subir `.env` ni `meli_tokens.json` a GitHub.

## Autenticacion Mercado Libre

La primera vez, cualquier comando que use Meli puede pedir autenticacion OAuth:

```bash
./venv/bin/python meli_client.py
```

El script imprime una URL de autorizacion. Abrirla, autorizar con la cuenta de Zipp y copiar el `code` de la URL de retorno. Eso crea `meli_tokens.json` localmente.

Importante: si Meli bloquea lectura de ordenes con `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`, revisar en DevCenter que la app tenga el permiso funcional de ventas/envios y reautorizar.

Chequeo rapido:

```bash
./venv/bin/python scripts/process_meli_to_shopify_stock.py --check-permissions
```

## Webhooks

Arrancar el catcher:

```bash
PORT=3000 node scripts/shopify_webhook_catcher.js
```

Endpoints:

```text
POST /webhooks/shopify/orders-create
POST /webhooks/meli/orders
```

El catcher:

- Crea `data/stock_sync.db` si no existe.
- Guarda cada webhook en `raw_events`.
- Para Shopify, crea una `stock_task` por line item.
- Para Meli, guarda la notificacion y el procesador luego lee la orden real por API.

Configurar en Shopify un webhook `orders/create` apuntando a:

```text
https://TU-DOMINIO/webhooks/shopify/orders-create
```

Configurar en Mercado Libre la notificacion de ordenes apuntando a:

```text
https://TU-DOMINIO/webhooks/meli/orders
```

## Procesar Shopify -> Meli

Dry-run:

```bash
./venv/bin/python scripts/process_shopify_to_meli_stock.py --limit 10
```

Apply:

```bash
./venv/bin/python scripts/process_shopify_to_meli_stock.py --apply --limit 10
```

Flujo:

```text
Venta Shopify
-> webhook orders/create
-> raw_events + stock_tasks en SQLite
-> dry-run busca SKU en Meli y deja ready_to_apply
-> apply actualiza available_quantity en Meli
-> synced
```

## Procesar Meli -> Shopify

Dry-run de webhooks pendientes:

```bash
./venv/bin/python scripts/process_meli_to_shopify_stock.py --limit 10
```

Dry-run de una orden puntual:

```bash
./venv/bin/python scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682
```

Apply:

```bash
./venv/bin/python scripts/process_meli_to_shopify_stock.py --apply --limit 10
```

Apply de una orden puntual ya revisada:

```bash
./venv/bin/python scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682 --apply --limit 1
```

Flujo:

```text
Venta Meli
-> webhook Meli
-> raw_events en SQLite
-> procesador lee /orders/{id}
-> crea stock_task source=meli por linea pagada
-> dry-run calcula stock objetivo Shopify
-> apply descuenta inventario Shopify
-> synced
```

## Automatizacion Recomendada

Mantener el catcher como servicio permanente. Ejemplo systemd:

```ini
[Unit]
Description=Zipp webhook catcher
After=network.target

[Service]
WorkingDirectory=/opt/zipp
Environment=PORT=3000
ExecStart=/usr/bin/node scripts/shopify_webhook_catcher.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Ejecutar procesadores con cron cada minuto o cada pocos minutos:

```cron
* * * * * cd /opt/zipp && ./venv/bin/python scripts/process_shopify_to_meli_stock.py --limit 10 >> logs/shopify_to_meli.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python scripts/process_shopify_to_meli_stock.py --apply --limit 10 >> logs/shopify_to_meli_apply.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python scripts/process_meli_to_shopify_stock.py --limit 10 >> logs/meli_to_shopify.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python scripts/process_meli_to_shopify_stock.py --apply --limit 10 >> logs/meli_to_shopify_apply.log 2>&1
```

Crear `logs/` en el servidor si se usa ese ejemplo:

```bash
mkdir -p logs
```

## Publicacion De Productos Shopify -> Meli

Modo prueba:

```bash
./venv/bin/python sync_products.py --limit 5
```

Publicar real:

```bash
./venv/bin/python sync_products.py --limit 5 --publish
```

Notas:

- Usa `sync_mappings.json` para no duplicar productos ya sincronizados.
- Si el producto ya esta mapeado y existe en Meli, actualiza precio/stock en vez de publicar otro.
- Para productos con bateria o terminos restringidos, evita Mercado Envios y deja retiro local.

## Estados De Tareas

```text
pending              Tarea recibida y pendiente de dry-run.
ready_to_apply       Dry-run exitoso, lista para aplicar.
synced               Cambio aplicado y confirmado.
needs_review         Requiere revision humana, no se toca stock.
retryable_error      Error temporal o de API, puede reintentarse.
skipped_not_in_meli  SKU existe en Shopify, pero no en Mercado Libre.
```

## Inspeccion De SQLite

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

## Seguridad Operacional

- No tocar stock si no hay SKU.
- No tocar stock si el SKU esta duplicado.
- No tocar stock si una variante no gestiona inventario desde Shopify.
- El apply Meli -> Shopify usa `max(stock - cantidad_vendida, 0)`.
- Reprocesar ordenes no deberia duplicar descuentos porque `task_id` es idempotente.
- Revisar `needs_review` antes de automatizar sin supervision.

## Pruebas Rapidas

```bash
./venv/bin/python test_meli_to_shopify_stock.py
./venv/bin/python test_sync_one.py
./venv/bin/python test_ai_sync.py
```

Algunas pruebas pueden necesitar variables reales de `.env` o tokens vigentes.

## Pendientes Recomendados

- Montar dominio estable para webhooks.
- Dejar catcher con systemd, pm2 o supervisor.
- Automatizar procesadores con cron/systemd timers.
- Agregar alertas para `needs_review` y `retryable_error`.
- Hacer backup periodico de `data/stock_sync.db`.
- V2 opcional: crear orden espejo en Shopify para reporting centralizado.

