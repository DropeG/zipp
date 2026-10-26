# Zipp

Este repositorio contiene las automatizaciones y skills para la tienda:

| Carpeta | Responsabilidad |
| --- | --- |
| [`product-sync/`](product-sync/README.md) | Publica productos Shopify → Mercado Libre, uno por ejecución. |
| [`prod/stock-sync/`](prod/stock-sync/README.md) | Mantiene el stock e importa ventas mediante webhooks y un worker. |
| [`market-price-review/`](market-price-review/SKILL.md) | Skill de estudio de precios: AliExpress y Mercado Libre Chile, sin modificar publicaciones. Ejecutor de servidor pendiente. |

Las automatizaciones ejecutables tienen su propio README, configuración de ejemplo, dependencias y
pruebas. La skill de estudio incluye sus instrucciones y la referencia de identificación del producto; requiere la skill externa `phi-browser` para navegar. No subas secretos, tokens ni estado temporal de ejecución.

## Entrada Rapida

Para montar el sincronizador de stock en servidor:

```bash
git clone git@github.com:DropeG/zipp.git
cd zipp/prod/stock-sync
cp .env.example .env
chmod 600 .env
docker compose build receiver worker python-tests node-tests
```

Despues sigue el manual especifico:

```text
prod/stock-sync/README.md
```
