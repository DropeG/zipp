# Plan de Desarrollo: Agente de Sincronización Shopify -> Mercado Libre

Este documento sirve para realizar el seguimiento del desarrollo paso a paso. La regla del proyecto es avanzar estrictamente de forma secuencial, validando cada paso antes de continuar.

---

## Estado del Proyecto

- [x] **Paso 1: Conexión base a la API de Shopify y obtención de productos (JSON)**
  - [x] Crear estructura base del proyecto (`requirements.txt`, `.env`).
  - [x] Escribir el cliente de conexión a Shopify (`shared/shopify_client.py`).
  - [x] Validar la conexión con credenciales reales y listar productos.
- [x] **Paso 2: Conexión base a la API de Mercado Libre (Autenticación y POST de prueba)**
  - [x] Diseñar el cliente para Mercado Libre.
  - [x] Implementar la autenticación de Mercado Libre (OAuth 2.0).
  - [x] Realizar una publicación de prueba (POST) en el entorno de pruebas de Mercado Libre.
- [x] **Paso 3: Lógica de IA para Mapeo y Optimización**
  - [x] Diseñar el prompt para mapear categorías de Shopify a las correctas de Mercado Libre.
  - [x] Implementar la optimización del título del producto para SEO en Mercado Libre usando la API de Gemini.
  - [x] Probar el mapeo de categorías con datos de ejemplo de Shopify.
- [x] **Paso 4: Integración del Flujo Principal del Agente**
  - [x] Unir la lectura de Shopify con el procesamiento de IA.
  - [x] Publicar automáticamente el producto adaptado en Mercado Libre.
  - [x] Controlar duplicados (evitar publicar dos veces el mismo producto de Shopify).
- [x] **Paso 5: Empaquetado como Skill de Antigravity y Reglas del Proyecto**
  - [x] Crear y estructurar la Skill personalizada `shopify-to-meli-one-by-one-sync`.
  - [x] Crear la guía de documentación para futuras Skills.
  - [x] Establecer las reglas del proyecto (`AGENTS.md`) para resolver compatibilidades y validaciones.
