# Protocolo De Documentacion De Automatizaciones

Este documento define como documentar automatizaciones nuevas dentro del repo Zipp.

La meta es que un humano o una IA pueda abrir una automatizacion y entender rapidamente:

- que hace
- que sistemas toca
- como se configura
- como se prueba localmente
- como se monta en servidor
- como se opera
- que falta

## Ubicacion Canonica

Cada automatizacion concreta vive en:

```text
automations/<automation-id>/
```

La documentacion especifica de esa automatizacion vive dentro de la misma carpeta:

```text
automations/<automation-id>/README.md
automations/<automation-id>/docs/
```

No dejar documentacion de una automatizacion concreta solo en `docs/` global. La carpeta global `docs/` es para reglas, convenciones y documentacion del repo completo.

## Estructura Recomendada

```text
automations/<automation-id>/
  README.md
  docs/
    HANDOFF.md
    ARCHITECTURE.md
    OPERATIONS.md
    LOCAL_TESTING.md
    TROUBLESHOOTING.md
    PENDING.md
```

Se pueden agregar documentos extra cuando ayuden, por ejemplo:

```text
HISTORICAL_CONTEXT.md
SECURITY.md
DATA_MODEL.md
```

No todos los documentos son obligatorios para una automatizacion pequena, pero si una automatizacion toca servicios externos, datos reales, dinero, ventas, stock o clientes, debe tener al menos:

- `README.md`
- `docs/LOCAL_TESTING.md`
- `docs/OPERATIONS.md`
- `docs/PENDING.md`

## Proposito De Cada Documento

### `README.md`

Entrada principal de la automatizacion.

Debe explicar:

- que problema resuelve
- que sistemas toca
- que datos lee
- que datos modifica
- estado actual
- comandos principales
- links a documentos internos

No debe ser el manual completo. Debe orientar y derivar.

### `docs/HANDOFF.md`

Manual de traspaso para montar o recibir la automatizacion.

Debe explicar:

- requisitos
- variables de entorno
- credenciales necesarias
- pasos de instalacion
- pasos para autenticar servicios externos
- como levantarla en servidor
- checklist final de traspaso

### `docs/ARCHITECTURE.md`

Explicacion tecnica de como funciona.

Debe explicar:

- flujo de datos
- scripts o servicios principales
- archivos de estado
- tablas o modelos usados
- decisiones importantes
- limites conocidos de la implementacion

### `docs/OPERATIONS.md`

Manual de operacion diaria o recurrente.

Debe explicar:

- comandos normales de ejecucion
- logs
- backups
- inspeccion de estado
- tareas recurrentes
- comandos seguros antes de comandos reales

### `docs/LOCAL_TESTING.md`

Manual para que un humano pruebe la automatizacion localmente imitando produccion.

Debe explicar:

- objetivo de la prueba local
- requisitos locales
- como levantar servicios locales
- como conectar servicios externos si aplica
- como generar o esperar eventos de prueba
- como revisar resultados locales
- como ejecutar pruebas seguras
- como aplicar cambios reales de manera limitada si la automatizacion lo requiere
- como limpiar configuracion temporal despues de probar

Este documento no es para diagnosticar fallas. Es para explicar el camino normal de prueba local.

### `docs/TROUBLESHOOTING.md`

Manual para diagnosticar y recuperarse cuando algo falla.

Debe explicar:

- errores conocidos
- sintomas
- causa probable
- comandos de revision
- accion recomendada
- cuando detener la automatizacion

### `docs/PENDING.md`

Lista de pendientes, riesgos y mejoras futuras.

Debe distinguir claramente:

- validado
- pendiente antes de produccion
- mejora futura
- riesgo conocido

No presentar algo pendiente como si ya existiera.

### Documentos Historicos O De Contexto

Usar documentos como `HISTORICAL_CONTEXT.md` cuando haya aprendizaje historico importante que no pertenezca al manual operativo.

Debe servir para entender decisiones pasadas, no para reemplazar documentacion actual.

## Regla Para IA

Cuando una IA documente una automatizacion:

1. Debe mirar primero la carpeta real de la automatizacion.
2. Debe documentar lo que existe, no lo que seria ideal.
3. Debe separar comportamiento validado, pendiente y supuesto.
4. Debe crear documentacion dentro de `automations/<automation-id>/docs/`.
5. Debe actualizar el `README.md` local para enlazar los documentos relevantes.
6. No debe mostrar carpetas incompletas o de estacionamiento como automatizaciones activas.
7. Si hay dudas, debe dejar la duda en `PENDING.md` o preguntar antes de afirmar.

## Regla Para El README Raiz

El `README.md` raiz solo debe mostrar automatizaciones activas o listas para ser presentadas.

Las automatizaciones futuras, incompletas o historicas pueden existir en el repo, pero no deben aparecer como flujo activo hasta que tengan documentacion minima y estado claro.

## Checklist Para Una Nueva Automatizacion

Antes de considerar una automatizacion documentada:

- [ ] Existe `automations/<automation-id>/README.md`.
- [ ] El README local explica objetivo, sistemas tocados y estado.
- [ ] Existe `docs/LOCAL_TESTING.md` si se puede probar localmente.
- [ ] Existe `docs/OPERATIONS.md` si se ejecuta manual o automaticamente.
- [ ] Existe `docs/PENDING.md` con limites y mejoras futuras.
- [ ] Los comandos documentados se ejecutan desde la raiz del repo o indican su carpeta exacta.
- [ ] Los secretos y archivos runtime no se suben a Git.
- [ ] El README raiz solo enlaza la automatizacion si ya esta presentable.
