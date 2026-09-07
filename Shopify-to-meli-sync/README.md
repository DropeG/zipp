# Shopify-to-meli-sync

Paquete autocontenido para revisar la automatización que publica **un producto
de Shopify a Mercado Libre Chile por ejecución**. Reúne las instrucciones de
la skill, su implementación Python, pruebas y el mapeo compartido de la
tienda.

## Cómo leerlo

1. Empieza por [`skill/SKILL.md`](skill/SKILL.md): explica cuándo se activa la
   skill y el recorrido completo de selección, validación y publicación.
2. Consulta `skill/references/`: contiene las reglas de negocio, el formato
   del payload, controles de políticas, calidad y recuperación de errores.
3. Revisa `implementation/automations/product-publishing/README.md`: detalla
   los comandos disponibles.
4. Usa `implementation/automations/product-publishing/tests/` para ver casos
   ejecutables que protegen las reglas más importantes.

## Estructura

```text
Shopify-to-meli-sync/
├── skill/                         Instrucciones para Codex y sus referencias.
└── implementation/
    ├── automations/product-publishing/
    │   ├── scripts/               Selector, contrato, payload y publicación.
    │   ├── tests/                 Pruebas sin llamadas reales a las APIs.
    │   └── sync_mappings.json     Mapeo Shopify ↔ Mercado Libre compartido.
    ├── automations/stock-sync/    Actualización posterior de inventario.
    └── shared/                    Clientes reutilizados de Shopify y Meli.
```

## Comportamiento principal

- Selecciona como máximo un producto activo con al menos una variante con
  precio y stock positivo.
- Un producto con variantes se publica como **un solo ítem Meli** que contiene
  todas sus variantes, incluso las que tienen stock cero.
- Mantiene el stock, precio, SKU e imágenes de Shopify; no inventa datos.
- Usa `sync_mappings.json` para evitar duplicar productos ya publicados.
- Valida localmente y contra Mercado Libre antes de crear una publicación.
- Si Meli no conserva todas las variantes, la publicación no se marca como
  exitosa ni se guarda un mapeo nuevo.

## Configuración local

```bash
cd Shopify-to-meli-sync/implementation
cp .env.example .env
# Completa las credenciales reales en .env (no lo subas al repositorio).
```

El archivo de tokens de Mercado Libre se guarda como
`implementation/meli_tokens.json` y tampoco se versiona. El mapeo
`sync_mappings.json` sí se incluye intencionalmente: esta automatización opera
la misma tienda compartida por ambos administradores.

## Verificación segura

Desde `Shopify-to-meli-sync/implementation`:

```bash
./venv/bin/python -m unittest discover \
  -s automations/product-publishing/tests -p 'test_*.py'

./venv/bin/python automations/product-publishing/scripts/one_by_one_sync.py \
  prepare --mode dry-run
```

El primer comando no usa cuentas reales. El segundo consulta la fuente y
prepara una sola publicación, pero no crea nada en Mercado Libre.

## Qué no se debe subir

- `.env` y `meli_tokens.json`.
- Archivos generados en `data/product-publishing/`.
- Journals, reservas, bloqueos, registros de calidad y archivos de bloqueo.

## Estado del paquete

Esta carpeta es un módulo de revisión y traspaso. Su contenido se mantiene
junto para que pueda revisarse sin navegar por el resto del repositorio.
