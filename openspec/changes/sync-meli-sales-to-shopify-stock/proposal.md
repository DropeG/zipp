## Why

Mercado Libre sales can reduce sellable stock before Shopify knows about them, which creates a risk of overselling in Shopify and any downstream channel that trusts Shopify inventory. The real sale for SKU `SOP-BAS-58` confirmed that Mercado Libre order access now works and that the system can resolve SKU and quantity from a paid order.

## What Changes

- Add a Mercado Libre -> Shopify stock synchronization capability for paid Mercado Libre orders.
- Process each Mercado Libre order line by SKU and quantity sold.
- Match each SKU to exactly one Shopify variant before changing inventory.
- Decrement Shopify inventory for the matched variant, clamping at zero and never inventing stock.
- Record each Mercado Libre order line as an idempotent stock task so repeated webhooks or reruns do not double-decrement stock.
- Preserve a dry-run mode that reads orders, resolves SKUs, and reports the intended Shopify stock change without modifying Shopify.
- Defer Shopify "mirror order" creation to a future change, while leaving the design compatible with it.

## Capabilities

### New Capabilities
- `meli-sales-stock-sync`: Synchronizes paid Mercado Libre sales into Shopify inventory by resolving order SKUs, creating idempotent tasks, and applying safe stock decrements.

### Modified Capabilities

None.

## Impact

- Affected scripts:
  - `scripts/shopify_webhook_catcher.js`
  - `scripts/process_meli_to_shopify_stock.py`
  - potentially shared Shopify/Meli request helpers if extracted during implementation
- Affected data:
  - `data/stock_sync.db`
  - `raw_events`
  - `stock_tasks`
  - `sync_logs`
- External systems:
  - Mercado Libre Orders API
  - Shopify Admin API inventory endpoints
- Operational behavior:
  - Mercado Libre paid sales become pending/ready/synced local tasks.
  - Shopify remains the stock source of truth after the Mercado Libre sale is applied.
