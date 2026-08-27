# Estructura Del Repositorio

Este repositorio es el hub de automatizaciones de Zipp. La regla principal es que la raiz se mantenga liviana y que cada automatizacion tenga su propia carpeta.

## Carpetas Principales

```text
automations/
  <automation-id>/
    README.md
    docs/
    scripts/
    tests/

shared/
  Clientes y helpers reutilizables entre automatizaciones.

data/
  Estado runtime local o de servidor. No versionar bases reales.

docs/
  Documentacion global del repositorio.

openspec/
  Planificacion de cambios, specs y tareas.
```

## Que Va En La Raiz

- `README.md`
- `.env.example`
- `.gitignore`
- `requirements.txt`
- `data/.gitkeep`
- `docs/`
- `automations/`
- `shared/`
- `openspec/`

La raiz no debe llenarse con scripts sueltos, notas de una sola automatizacion, dumps, tokens o reportes.

## Que Va En Una Automatizacion

Cada automatizacion usa esta forma:

```text
automations/<automation-id>/
  README.md                 Entrada principal de esa automatizacion.
  docs/HANDOFF.md           Traspaso si alguien debe operarla.
  docs/ARCHITECTURE.md      Flujos internos y decisiones tecnicas.
  docs/OPERATIONS.md        Comandos, logs, tareas recurrentes.
  docs/TROUBLESHOOTING.md   Errores comunes y recuperacion.
  docs/PENDING.md           Pendientes, riesgos, V2.
  scripts/                  Scripts ejecutables de esa automatizacion.
  tests/                    Pruebas de esa automatizacion.
```

No todos los documentos son obligatorios para automatizaciones pequenas, pero `README.md` si.

## Que Va En `shared/`

`shared/` contiene integraciones o helpers que pueden servir a mas de una automatizacion:

- Shopify
- Mercado Libre
- Gemini / AI
- resolucion de rutas del repo

Los flujos concretos quedan dentro de `automations/`.

## Estado Runtime

Estos archivos viven localmente o en el servidor, pero no en Git:

```text
.env
meli_tokens*.json
data/*.db
logs/
*.log
productos_listos*.json
reportes generados
imagenes temporales
```

Si un archivo es necesario como ejemplo, crear una version `.example` sin secretos ni datos reales.
