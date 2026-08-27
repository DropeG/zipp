# Pendientes: Stock Sync

## Para Produccion Inicial

- Montar en servidor estable.
- Configurar dominio publico estable para webhooks.
- Crear servicio permanente para `shopify_webhook_catcher.js`.
- Automatizar procesadores con cron o systemd timers.
- Configurar logs persistentes.
- Configurar backup periodico de `data/stock_sync.db`.
- Revisar manualmente `needs_review` antes de dejar apply sin supervision.

## Hardening Recomendado

- Validar firma/HMAC de webhooks Shopify.
- Agregar validacion mas fuerte de notificaciones Mercado Libre.
- Agregar alertas para `needs_review` y `retryable_error`.
- Agregar reconciliacion periodica entre Shopify y Meli.
- Agregar monitoreo de freshness: ultimo webhook recibido, ultima tarea aplicada, errores recientes.
- Agregar rotacion o manejo mas formal de logs.

## Fuera De Alcance V1

- Crear orden espejo de Mercado Libre en Shopify.
- Sincronizar datos de cliente, despacho, pago, impuestos o fulfillment desde Meli a Shopify.
- Revertir automaticamente stock por cancelaciones Meli.
- Resolver multi-bodega o multi-location avanzado.
- Publicar productos nuevos como parte del flujo de stock.

## Mejoras Futuras

- V2 de orden espejo en Shopify para reporting centralizado.
- Flujo de cancelaciones/devoluciones.
- Dashboard de estado de cola.
- Comando unico tipo `stock-sync doctor`.
- Configuracion de politica por SKU o categoria.
- Tests de integracion con fixtures mas completos de webhooks Shopify y Meli.
