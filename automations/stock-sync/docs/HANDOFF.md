# Handoff De Servidor: Stock Sync

Manual para montar el sincronizador Shopify <-> Mercado Libre en servidor.

## 1. Preparar Servidor

```bash
cd /opt
git clone https://github.com/DropeG/zipp.git
cd zipp

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
mkdir -p data logs
```

Requisitos:

- Python 3.11 o superior.
- Node.js 22 o superior.
- Credenciales Shopify Admin API.
- App Mercado Libre con permisos de publicacion y ventas/envios.
- Dominio publico o tunnel estable para recibir webhooks.

## 2. Configurar `.env`

Editar `/opt/zipp/.env`:

```text
SHOPIFY_SHOP_URL=...
SHOPIFY_ACCESS_TOKEN=...
SHOPIFY_API_VERSION=2024-04
MELI_APP_ID=...
MELI_CLIENT_SECRET=...
MELI_REDIRECT_URI=https://localhost
GEMINI_API_KEY=...
```

No subir `.env` a GitHub.

## 3. Autenticar Mercado Libre

```bash
cd /opt/zipp
./venv/bin/python shared/meli_client.py
```

El script imprime una URL de autorizacion. Abrirla con la cuenta de Zipp, autorizar y pegar el `code` que viene en la URL de retorno. Esto crea `meli_tokens.json` en la raiz del repo.

Validar permisos de ordenes:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --check-permissions
```

Si aparece `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`, revisar en DevCenter que la app tenga permiso funcional de ventas/envios y reautorizar.

## 4. Levantar Webhook Catcher

Prueba manual:

```bash
PORT=3000 node automations/stock-sync/scripts/shopify_webhook_catcher.js
```

Endpoints:

```text
POST /webhooks/shopify/orders-create
POST /webhooks/meli/orders
```

El catcher crea `data/stock_sync.db` si no existe y guarda webhooks en `raw_events`.

## 5. Configurar Webhooks

Shopify:

```text
https://TU-DOMINIO/webhooks/shopify/orders-create
```

Mercado Libre:

```text
https://TU-DOMINIO/webhooks/meli/orders
```

## 6. Probar Sin Tocar Stock

Shopify -> Mercado Libre:

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10
```

Mercado Libre -> Shopify:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10
```

Orden Meli puntual:

```bash
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --order-id 2000018107143682
```

## 7. Aplicar Cambios Reales

Usar `--apply` solo despues de revisar el dry-run.

```bash
./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 10
./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10
```

## 8. Automatizar

Servicio `systemd` para el catcher:

```ini
[Unit]
Description=Zipp stock webhook catcher
After=network.target

[Service]
WorkingDirectory=/opt/zipp
Environment=PORT=3000
ExecStart=/usr/bin/node automations/stock-sync/scripts/shopify_webhook_catcher.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Cron sugerido:

```cron
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --limit 10 >> logs/shopify_to_meli.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_shopify_to_meli_stock.py --apply --limit 10 >> logs/shopify_to_meli_apply.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --limit 10 >> logs/meli_to_shopify.log 2>&1
* * * * * cd /opt/zipp && ./venv/bin/python automations/stock-sync/scripts/process_meli_to_shopify_stock.py --apply --limit 10 >> logs/meli_to_shopify_apply.log 2>&1
```

## Checklist Final

- [ ] Repo clonado en `/opt/zipp`.
- [ ] Dependencias instaladas.
- [ ] `.env` creado con credenciales reales.
- [ ] `meli_tokens.json` creado con cuenta Zipp.
- [ ] `--check-permissions` OK.
- [ ] Catcher corriendo en servidor.
- [ ] Webhook Shopify configurado.
- [ ] Webhook Mercado Libre configurado.
- [ ] Dry-run sin errores criticos.
- [ ] `ready_to_apply` revisado.
- [ ] Apply automatico activado.
- [ ] Logs y backup de SQLite configurados.
