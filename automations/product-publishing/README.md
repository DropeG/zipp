# Product Publishing Shopify -> Mercado Libre

Automatizacion/herramientas para publicar productos de Shopify en Mercado Libre Chile usando datos de Shopify, helpers de Mercado Libre y optimizacion con Gemini.

## Estado

Herramientas existentes organizadas desde la raiz antigua. La documentacion profunda queda pendiente; por ahora esta carpeta preserva los scripts y notas historicas.

## Scripts

```text
scripts/sync_products.py
scripts/publish_payloads.py
scripts/fetch_meli_requirements.py
scripts/fix_meli_cover_with_external_bg.py
```

## Estado Local Versionado

```text
sync_mappings.json
```

Este archivo evita duplicar publicaciones ya sincronizadas.

## Notas Historicas

```text
docs/HISTORICAL_PLAN.md
```

## Comandos Base

Modo prueba:

```bash
./venv/bin/python automations/product-publishing/scripts/sync_products.py --limit 5
```

Publicar real:

```bash
./venv/bin/python automations/product-publishing/scripts/sync_products.py --limit 5 --publish
```
