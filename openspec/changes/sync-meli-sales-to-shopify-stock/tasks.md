## 1. Data Model and Task Queue

- [x] 1.1 Extend `process_meli_to_shopify_stock.py` schema setup to support Mercado Libre stock task fields needed for Shopify apply and audit.
- [x] 1.2 Add idempotent task creation for paid Mercado Libre order lines using stable `source = 'meli'` task ids.
- [x] 1.3 Ensure repeated Mercado Libre notifications or repeated `--order-id` runs do not create duplicate tasks or double-decrement stock.
- [x] 1.4 Record non-paid, missing-order, and unreadable-order cases in `sync_logs` without creating automatically applicable tasks.

## 2. Mercado Libre Order Resolution

- [x] 2.1 Keep direct SKU extraction from Mercado Libre order items.
- [x] 2.2 Keep fallback SKU extraction from Mercado Libre item or variation when the order item does not include a SKU.
- [x] 2.3 Mark lines with missing SKU, missing item id, missing variation SKU, or missing variation as `needs_review`.
- [x] 2.4 Preserve `--check-permissions` and explicit `--order-id` flows for operational testing.

## 3. Shopify Variant Matching

- [x] 3.1 Add Shopify API helpers to list/search variants by exact SKU.
- [x] 3.2 Detect and store the matched Shopify variant id and inventory item id when exactly one variant matches.
- [x] 3.3 Mark no-match cases as `skipped_not_in_shopify` or `needs_review` without changing stock.
- [x] 3.4 Mark duplicate-SKU matches as `needs_review` without changing stock.
- [x] 3.5 Detect variants that cannot be inventory-managed and log them as reviewable.

## 4. Dry-Run Processing

- [x] 4.1 In default mode, process pending Mercado Libre tasks without writing to Shopify.
- [x] 4.2 Fetch current Shopify stock at the configured active location for each matched variant.
- [x] 4.3 Compute and store/report `target_stock = max(current_stock - quantity_sold, 0)`.
- [x] 4.4 Mark valid tasks `ready_to_apply` with SKU, quantity, Shopify variant, current stock, target stock, and Mercado Libre order context.
- [x] 4.5 Log when quantity sold exceeds current Shopify stock and target is clamped to zero.

## 5. Apply Processing

- [x] 5.1 Add `--apply` mode for `source = 'meli'` tasks in `ready_to_apply`.
- [x] 5.2 Re-read fresh Shopify stock immediately before each inventory update.
- [x] 5.3 Set Shopify inventory to the fresh computed target stock.
- [x] 5.4 Confirm Shopify reports the expected stock after update.
- [x] 5.5 Mark confirmed tasks `synced` and write stock-before/stock-after audit logs.
- [x] 5.6 Mark Shopify API errors as retryable and confirmation mismatches as `needs_review`.

## 6. Verification

- [x] 6.1 Add focused tests or fixtures for paid order task creation, duplicate notification handling, missing SKU, no Shopify match, duplicate Shopify SKU, dry-run target calculation, and apply confirmation.
- [x] 6.2 Run a dry-run against real Mercado Libre order `2000018107143682` for SKU `SOP-BAS-58` and verify expected target stock is reported.
- [x] 6.3 Apply only a single known ready task after dry-run output is reviewed.
- [x] 6.4 Re-run the same order after apply and verify it does not decrement Shopify stock a second time.
- [x] 6.5 Update `STOCK_SYNC_CONTEXT.md` with the validated Mercado Libre -> Shopify flow and operating commands.
