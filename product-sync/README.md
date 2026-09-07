# Product Sync: Shopify → Mercado Libre

Automatización para publicar un producto de Shopify en Mercado Libre Chile por
ejecución. Shopify es la fuente de verdad; Mercado Libre es el destino.

## Estructura

```text
product-sync/
├── SKILL.md              Procedimiento completo de la automatización.
├── README.md             Esta guía de revisión y uso.
├── .env.example          Variables requeridas, sin credenciales reales.
├── requirements.txt      Dependencias Python.
├── references/           Reglas de negocio, payload, calidad y políticas.
├── scripts/              Selector, validación, publicación y recuperación.
├── shared/               Clientes Shopify y Mercado Libre.
├── state/
│   └── sync_mappings.json  Mapeo compartido Shopify ↔ Meli.
└── tests/                Pruebas sin llamadas reales a las APIs.
```

## Cómo funciona

1. Selecciona un producto activo con al menos una variante con precio y stock
   positivo.
2. Prepara un solo payload, usando datos e imágenes de Shopify.
3. Para un producto con variantes, publica un solo ítem Meli con todas las
   variantes, incluidas las que tienen stock cero.
4. Valida el payload localmente y luego contra Mercado Libre antes de crear.
5. Verifica que el ítem quedó activo y que conserva las variantes esperadas.
6. Guarda el mapeo para no volver a publicar el mismo producto.

Lee [`SKILL.md`](SKILL.md) para el procedimiento y las reglas completas.

## Configuración

```bash
cd product-sync
cp .env.example .env
# Completa .env con las credenciales reales; nunca lo subas a Git.
```

El token renovable de Mercado Libre se guarda en `product-sync/meli_tokens.json`
y no se versiona. `state/sync_mappings.json` sí se versiona intencionalmente,
porque ambos administradores operan la misma tienda y cuenta de Mercado Libre.

## Verificación segura

```bash
PYTHONPATH=product-sync ./venv/bin/python -m unittest discover \
  -s product-sync/tests -p 'test_*.py'

./venv/bin/python product-sync/scripts/one_by_one_sync.py prepare --mode dry-run
```

El segundo comando prepara una sola publicación, pero no crea ni modifica una
publicación de Mercado Libre.

## Estado local que no se sube

- `state/work/`
- `state/sync_blocked_products.json`
- `state/publication_journal.json`
- `state/selection_reservations.json`
- `state/publication_quality_records.json`
- `state/.one_by_one.lock`
- `.env` y `meli_tokens.json`
