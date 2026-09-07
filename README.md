# Zipp

Este repositorio contiene dos automatizaciones independientes para la tienda:

| Carpeta | Responsabilidad |
| --- | --- |
| [`product-sync/`](product-sync/README.md) | Publica productos Shopify → Mercado Libre, uno por ejecución. |
| [`prod/stock-sync/`](prod/stock-sync/README.md) | Mantiene el stock e importa ventas mediante webhooks y un worker. |

Cada carpeta tiene su propio README, configuración de ejemplo, dependencias y
pruebas. No subas secretos, tokens ni estado temporal de ejecución.
