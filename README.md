# Zipp Automations

Repositorio general para automatizaciones operativas de Zipp.

Este repo no representa una sola herramienta. Cada automatizacion vive en su propia carpeta bajo `automations/`, con sus scripts, pruebas y documentacion local. La primera automatizacion lista para traspaso es el sincronizador de stock Shopify <-> Mercado Libre.

## Automatizaciones Activas

| Automatizacion | Carpeta | Estado |
| --- | --- | --- |
| Stock sync Shopify <-> Mercado Libre | `automations/stock-sync/` | Funciona localmente; pendiente montaje en servidor |

Las automatizaciones futuras o incompletas no se listan como activas hasta que tengan estado y documentacion minima clara.

## Entrada Rapida

Para montar el sincronizador de stock en servidor:

```bash
git clone https://github.com/DropeG/zipp.git
cd zipp

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
```

Despues sigue el manual especifico:

```text
automations/stock-sync/docs/HANDOFF.md
```

## Estructura Del Repo

```text
automations/
  stock-sync/             Sincronizador de stock Shopify <-> Mercado Libre.
  <automation-id>/        Futuras automatizaciones documentadas cuando esten activas.

shared/                   Clientes y helpers reutilizables.
data/                     Estado runtime local/servidor, no versionado salvo .gitkeep.
docs/                     Documentacion global del repo.
openspec/                 Propuestas, specs y tareas de cambios.
```

Mas detalle: `docs/REPO_STRUCTURE.md`.

## Archivos Que No Se Suben

Nunca subir secretos ni estado runtime:

```text
.env
meli_tokens*.json
data/*.db
logs/
*.log
reportes generados
imagenes temporales
```

`.env.example` si se versiona porque no contiene secretos reales.

## Convencion Para Futuras Automatizaciones

Cada nueva automatizacion debe vivir en:

```text
automations/<automation-id>/
  README.md
  docs/
  scripts/
  tests/
```

Cada README local debe explicar:

- que problema resuelve
- que sistemas toca
- que datos lee y modifica
- como probar en modo seguro
- como ejecutar en modo real
- que falta o que riesgos conocidos tiene

Protocolo completo: `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md`.

## Documentacion Global

- `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md`: regla para documentar futuras automatizaciones.
- `docs/REPO_STRUCTURE.md`: convenciones de carpetas y responsabilidades.
- `docs/SERVER_BASELINE.md`: base recomendada para montar automatizaciones en servidor.
- `automations/stock-sync/docs/HANDOFF.md`: manual de traspaso del sincronizador.
