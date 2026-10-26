---
name: zipp-market-price-review
description: Revisa sin preguntas los precios del catálogo activo de Zipp, un producto por ejecución, o un SKU indicado para pruebas. Busca referencias en AliExpress, compara Mercado Libre Chile y genera reportes para revisión humana. No modifica publicaciones ni precios.
---

# Revisión de precios de Zipp

Procesa automáticamente un producto pendiente por ejecución, sin necesitar un SKU ni intervención humana. Las ejecuciones sucesivas recorren el catálogo activo de Shopify. El objetivo es comparar los precios publicados de Zipp con ofertas verificables de Mercado Libre Chile para que sus dueños decidan qué hacer.

## Alcance

- Solo lectura: no cambiar precios, stock, publicaciones ni configuraciones, y no contactar vendedores.
- El mercado de referencia incluye únicamente ofertas de Mercado Libre Chile. AliExpress sirve para identificar el producto y obtener una referencia de adquisición o reposición; sus precios no se mezclan con los precios de venta de competidores chilenos.
- Para navegar, usa la skill `phi-browser` en su espacio de agente y respeta su cierre de sesión. Usa conectores o APIs disponibles para lecturas del catálogo cuando resulten adecuados.
- No hagas preguntas ni esperes respuestas, confirmaciones o acciones del usuario. Usa los datos disponibles y marca los faltantes o contradicciones en el reporte; no inventes costos ni atributos. Esta regla también aplica al usar product-reference-finder y al navegar: si aparece un login, captcha o verificación humana, registra la fuente como no verificable y continúa con las fuentes accesibles, sin intentar eludir el bloqueo.

## Selección y avance automático

- Sin SKU ni enlace, consulta todos los productos Shopify con estado `ACTIVE`, recorriendo la paginación completa. No confundas activo con tener stock: incluye también los de stock cero e informa esa condición. Ordena por ID de producto para que el avance sea estable.
- Selecciona el primer producto pendiente del ciclo actual. Usa estado propio de esta revisión; los mapeos de product-sync sirven para localizar publicaciones, pero no indican que un producto ya tenga estudio de mercado. Revalida que siga activo antes de analizarlo.
- Revisa sus variantes por separado cuando cambien características o precio; conserva ID de producto, ID de variante y SKU en cada resultado. Si el SKU falta, identifica la variante por su ID. No mezcles variantes ni pidas elegir una.
- Guarda cada reporte y sus fotos en almacenamiento persistente del entorno de ejecución. Guarda también un registro del ciclo con IDs, fecha, estado (`completado`, `parcial` o `error`) y ruta de reporte; marca completado solo después de guardar el reporte. Reanuda desde ese registro tras una interrupción, sin rehacer variantes terminadas. No ejecutes dos revisiones simultáneas sobre el mismo estado.
- Un costo desconocido, ausencia de referencia exacta o poca competencia producen un reporte parcial; no bloquean el recorrido. Un error de un producto se registra y la siguiente ejecución avanza al siguiente pendiente, sin reintentar indefinidamente el mismo producto. Si falla el acceso al catálogo, registra el fallo de ejecución sin afirmar que se terminó el ciclo.
- Cuando no queden pendientes, informa «ciclo terminado», con cantidades de completados, parciales y errores. No reinicies automáticamente otro ciclo; una nueva revisión debe iniciarse explícitamente o mediante la configuración del ejecutor.
- Un SKU o enlace explícito conserva el modo de prueba puntual: analiza solo ese producto o variante. No marques el resto del catálogo como revisado.

Estas instrucciones definen el comportamiento del agente. El servidor necesita un ejecutor que lo invoque sucesivamente y conserve el estado y los reportes; este archivo no instala ni programa ese servicio.

## 1. Establecer qué vende Zipp

Lee la ficha de Shopify y registra SKU, producto, variante, descripción, fotos, precio vigente y URL. Revisa también su publicación vinculada de Mercado Libre, si puedes verificar el vínculo por los datos existentes; no lo inventes a partir del título.

Usa [product-reference-finder](references/product-reference-finder.md) como base para identificar marca, modelo y variante mediante fotos y especificaciones. Reutiliza una identificación anterior si su evidencia sigue correspondiendo a la variante. Ejecuta su búsqueda de referencia en AliExpress para el mismo modelo y variante y registra el precio vigente. Reutiliza la identidad comprobada, pero vuelve a verificar precios y disponibilidad. Si no encuentras coincidencia, continúa el estudio y declara que falta esa referencia; no uses un producto parecido como costo del nuestro.

Resume los atributos que distinguen este producto: por ejemplo, potencia y puertos de un cargador, longitud y conectores de un cable, capacidad o contenido del paquete. No confundas el proveedor «Zipp Chile» con la marca fabricante. Si no puedes confirmar marca o modelo, conserva esa incertidumbre al clasificar las ofertas.

Registra por separado el precio actual de Shopify y el de Mercado Libre de Zipp. Si alguno no se puede leer, indícalo y continúa con la información verificable.

## 2. Establecer el costo

Distingue siempre:

- **Compra real:** importe histórico respaldado por pedido, factura, registro del catálogo o dato del usuario, con fecha, cantidad y moneda. Busca en los datos del producto disponibles y registros de compra expresamente aportados o autorizados. No presentes un campo de costo sin fecha ni alcance como costo completo confirmado. No accedas a correo u otros archivos personales por defecto.
- **Referencia AliExpress actual:** precio publicado hoy del modelo y variante correctos. No demuestra cuánto pagó Zipp ni quién fue su proveedor original. Registra enlace, vendedor, fecha, cantidad, moneda, destino Chile, envío e impuestos visibles. Excluye precios de bienvenida, cupones personales y opciones «desde» que no correspondan. Si conviertes a CLP, verifica y cita la tasa y su fecha; no inventes conversión.

Para el stock existente prioriza el costo histórico. Para evaluar reposición usa la referencia actual. Nunca sumes ambos como si fueran dos costos de una unidad.

Calcula el **costo puesto en Chile por unidad** cuando haya datos: compra + envío internacional + importación y cargos aplicables, dividido por las unidades correspondientes. Reparte cargos compartidos con un criterio explícito. Identifica los componentes incluidos para no duplicar impuestos o fletes. Mantén una base consistente respecto de impuestos recuperables; no supongas su tratamiento ni tasas.

Busca comisiones y cargos vigentes de la publicación vinculada y costos de venta disponibles: cargo fijo, envío a cargo de Zipp y embalaje. No uses tarifas genéricas como si fueran las reales. Si falta información, continúa sin preguntar y entrega el reporte con los faltantes señalados y la rentabilidad sin determinar cuando corresponda. Un costo desconocido no vale cero.

## 3. Buscar y verificar competencia

Busca primero marca + modelo y luego nombres alternativos y características distintivas. Usa la búsqueda de Mercado Libre Chile y un buscador externo restringido al sitio; usa búsqueda por imagen cuando aporte pistas. Abre los candidatos para revisar la oferta y variante concretas: un resumen de búsqueda o un precio «desde» no basta.

Clasifica cada oferta:

- **Iguales:** mismo producto por modelo o por coincidencia del diseño físico y todas las características y funciones relevantes. En productos genéricos no exijas conocer al fabricante: contrasta fotos, conectores, construcción y especificaciones; una foto compartida por sí sola no basta. Un cambio solo de color no obliga a separarlo de este grupo, pero debe indicarse. No afirmes que comparten fabricante si no está comprobado.
- **Parecidos:** distinto producto, con el mismo formato de uso y las características y funciones que busca el comprador. Indica qué coincide y qué cambia. Para un cable llavero 4 en 1, verifica las cuatro combinaciones de conectores, carga y datos; no basta con que sea un cable pequeño. Si falta evidencia de una función esencial, no lo incluyas en la comparación de precios.

Evalúa el segmento por la necesidad del comprador, no solo por watts o cantidad de puertos. Contrasta prestaciones, reparto de potencia cuando corresponda, protocolos, construcción, evidencia de calidad y seguridad, garantía, accesorios y entrega. No asignes gama baja, media o premium por precio o reputación de marca solamente. Si no hay evidencia suficiente, deja la gama «sin determinar». No descartes por esto equivalentes útiles, pero explica diferencias que puedan justificar precio.

Busca ambos grupos para mostrar el panorama de competencia, priorizando los iguales. Muestra hasta **8 ofertas en total**, salvo que el usuario indique otro límite. Elige ofertas verificadas de distintos vendedores, precios y opciones de entrega, sin rellenar con coincidencias débiles. Si hay menos, muestra las que encuentres.

Ejemplo acordado: para CAB-ZIP-6E-GRIS-75, el cable 4 en 1 de Tienda Chen Yi Fei pertenece a «Iguales», con diferencia de color; el llavero de Gy My Deco Store pertenece a «Parecidos», por su pantalla y otras diferencias. Estos ejemplos orientan la clasificación, pero hay que verificar nuevamente la oferta y sus datos en cada consulta.

Excluye ofertas de Zipp, productos usados o reacondicionados cuando Zipp venda nuevo y publicaciones inactivas. No cuentes dos veces la misma oferta. Una página de catálogo y sus vendedores no son observaciones adicionales del mismo precio: identifica vendedor y oferta, conservando el enlace específico cuando exista.

Por oferta, registra:

- enlace, vendedor y variante o presentación;
- precio publicado vigente en CLP para esa variante;
- envío a la ubicación consultada si está disponible, o «sin verificar»;
- condiciones del precio, como cupón, membresía, medio de pago o compra mínima;
- evidencia de coincidencia, diferencias relevantes y foto de la variante analizada.

Compara precios de compra de una unidad sin beneficios personales condicionados. No uses la cuota como precio total. Para packs, muestra cantidad y precio por unidad, y mantenlos como equivalentes si cambia la presentación. No sumes un envío desconocido como si fuera gratis. Señala envíos internacionales y plazos visibles cuando afecten la comparación local.

Termina cuando tengas una muestra útil y las búsquedas adicionales repitan las mismas ofertas, o cuando las vías relevantes no aporten nuevas pistas. Si hay pocas ofertas, repórtalas con esa limitación. Un bloqueo de acceso significa «no pude verificar», no «no existe competencia».

## 4. Reporte visual y breve

Genera un reporte en español por variante analizada, con fecha de consulta. En ejecución de servidor guárdalo con sus fotos en almacenamiento persistente; en una prueba por chat muéstralo también en la respuesta. Usa rutas relativas a las fotos en el reporte guardado para mantenerlo portable, y rutas absolutas al mostrarlas en el chat. La información necesaria para decidir debe verse en la respuesta sin abrir enlaces. Usa exactamente estas tres secciones:

### 1. Nuestro producto

Foto visible, nombre, SKU, variante, características esenciales y precio actual por canal. Si ambos canales tienen el mismo precio, escríbelo una sola vez. Mantén el enlace de origen en el nombre. Añade un resumen compacto de costos:

| Concepto | Importe por unidad | Evidencia / estado |
|---|---:|---|
| Compra real | Importe o «desconocido» | Fuente y fecha |
| AliExpress actual | Importe o «no verificado» | Enlace y variante; referencia de reposición |
| Puesto en Chile | Importe o «incompleto» | Componentes incluidos y faltantes |
| Costos de venta por canal | Importe o «incompletos» | Comisión, cargo fijo, envío y embalaje |

No agregues columnas ni explicaciones extensas si no aportan a la decisión.

### 2. Comparación con mercado

Divide en **Iguales** y **Parecidos**. Si un grupo está vacío, indícalo en una línea. Muestra cada grupo en una tabla con columnas **Foto | Producto / vendedor | Precio | Coincidencias y diferencias | Entrega**. Mantén las celdas breves y el nombre enlazado a la oferta. Identifica cada foto con un número. Si las imágenes no se renderizan dentro de la tabla, muestra una galería numerada inmediatamente debajo, usando esos números para vincular foto y fila.

En entrega incluye origen, plazo, envío e impuestos adicionales cuando apliquen. Un costo desconocido se expresa como «sin verificar»; evita presentar el precio base como total.

Usa capturas locales del navegador para las fotos: las imágenes remotas de Shopify y Mercado Libre ya fallaron al mostrarse en este chat. Abre la foto principal ampliada de la variante, no una miniatura de resultados, o captura el área del producto sin datos personales. Usa una fuente de al menos 600 px de lado cuando esté disponible y recorta la captura al producto; no amplíes una miniatura de 140 px para simular calidad. Guarda las capturas en una carpeta de reportes fuera de /tmp y de carpetas temporales del sistema, y enlázalas con Markdown: ruta relativa en el reporte guardado y absoluta en el chat. Inspecciona los archivos antes de entregarlos. No descargues imágenes para eludir restricciones de visualización. Si no puedes obtener una captura, indícalo sin inventarla.

### 3. Análisis y consejo

En dos a cuatro frases: si nuestro precio parece alto, razonable o bajo frente a lo observado, el motivo principal y una recomendación concreta: **precio actual → precio sugerido**, diferencia en pesos y porcentaje. Si conviene mantener, indica cambio $0. Fundamenta el precio en las ofertas más cercanas y sus costos de entrega, sin copiar automáticamente el mínimo. Cuando la muestra sea insuficiente, recomienda mantener provisionalmente y explica qué falta. Distingue el precio competitivo del resultado económico. Con costos completos calcula, por canal y para el precio actual y sugerido: **contribución unitaria = ingreso de la venta en la base tributaria utilizada − costo puesto en Chile − comisión − cargo fijo − envío a cargo de Zipp − embalaje**. Si expresas margen, divide la contribución por el ingreso en esa misma base; no lo confundas con recargo sobre costo ni con utilidad neta. No cuentes dos veces cargos ya descontados del ingreso.

Si solo conoces el precio AliExpress, muestra la diferencia entre venta y esa referencia, pero no la llames margen ni ganancia. Con faltantes, declara «rentabilidad no determinada» y conserva el precio sugerido como objetivo comercial provisional. Separa escenarios basados en compra histórica y reposición estimada; nunca presentes una estimación como costo confirmado. No recomiendes por debajo del punto de equilibrio conocido como operación rentable; si el mercado no permite cubrir costos, dilo. No inventes un margen objetivo: usa el que indique el usuario. Nunca modifiques publicaciones.

Considera envío, impuestos y plazo antes de juzgar diferencias de precio. No mezcles iguales y parecidos en un promedio. Usa rangos o porcentajes solo si aclaran la decisión; no incluyas mediana ni cálculos por rutina. Los precios publicados no prueban ventas o demanda.

No agregues un diario de búsquedas, candidatos descartados ni advertencias repetidas. Menciona solo limitaciones que puedan cambiar la conclusión, junto al dato afectado o en el análisis. Si no hay ofertas verificables, entrega nuestro producto y explica brevemente que no alcanza para evaluar el precio.
