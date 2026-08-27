# Inventory Reports

Herramientas de inventario y reportes Excel para Shopify.

## Estado

Scripts existentes organizados en una automatizacion propia. La documentacion profunda queda pendiente.

## Scripts

```text
scripts/update_stock_from_excel.py
scripts/generar_reporte_excel.py
scripts/verificar_stock_shopify.py
```

## Ejemplos

Actualizar stock desde Excel en modo simulacion:

```bash
./venv/bin/python automations/inventory-reports/scripts/update_stock_from_excel.py --excel "/ruta/al/archivo.xlsx"
```

Aplicar cambios reales:

```bash
./venv/bin/python automations/inventory-reports/scripts/update_stock_from_excel.py --excel "/ruta/al/archivo.xlsx" --apply
```
