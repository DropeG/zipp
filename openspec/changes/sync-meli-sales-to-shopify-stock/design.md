## Context

The repository already has a local stock synchronization foundation:

- `scripts/shopify_webhook_catcher.js` stores Shopify and Mercado Libre webhook payloads in `data/stock_sync.db`.
- `scripts/process_shopify_to_meli_stock.py` processes Shopify-originated sales through `stock_tasks`, supports dry-run, and applies stock to Mercado Libre.
- `scripts/process_meli_to_shopify_stock.py` can read Mercado Libre raw events or a specific order, fetch `/orders/{id}`, and resolve SKU/quantity in dry-run.

The real Mercado Libre order `2000018107143682` for SKU `SOP-BAS-58` showed that order permissions are now usable, the order status is `paid`, and the line item exposes SKU and quantity directly.

Shopify remains the inventory source of truth. Mercado Libre sales must be reflected in Shopify quickly enough to avoid selling unavailable stock in Shopify or any future connected channel.

## Goals / Non-Goals

**Goals:**

- Convert paid Mercado Libre order lines into idempotent local stock tasks.
- Resolve each Mercado Libre line to exactly one Shopify variant by SKU.
- In dry-run, show the current Shopify stock and the intended post-sale stock without writing to Shopify.
- In apply mode, decrement Shopify inventory by the Mercado Libre quantity sold and confirm the resulting stock.
- Preserve auditability through `sync_logs` and task status transitions.
- Treat ambiguous, missing, duplicate, canceled, or unsupported data as reviewable rather than applying risky stock changes.

**Non-Goals:**

- Do not create Shopify mirror orders in this change.
- Do not sync customer, shipping, payment, tax, fee, or fulfillment details from Mercado Libre into Shopify.
- Do not reconcile historical orders automatically beyond the operator-provided limits/order ids.
- Do not solve multi-location allocation beyond using the configured/active Shopify location already used by local tooling.

## Decisions

### Use direct Shopify inventory adjustment for V1

Mercado Libre sales will decrement Shopify inventory directly instead of creating a Shopify order.

Rationale: direct adjustment is the smallest safe behavior needed to prevent overselling. Creating mirror orders is attractive for centralized sales reporting, but it can trigger Shopify order webhooks, customer notifications, fulfillment flows, accounting integrations, and analytics side effects. Those need a separate design.

Alternative considered: create a Shopify order or completed draft order for each Mercado Libre sale and let Shopify reserve/decrement inventory. This is better for unified sales records, but riskier as a first production step.

### Reuse `stock_tasks` as the durable work queue

Each Mercado Libre order line will become a `stock_tasks` row with `source = 'meli'`.

Task ids will be stable and idempotent, for example:

```text
meli:<order_id>:<line_index>:<item_id>:<variation_id-or-no-variation>
```

Rationale: the Shopify -> Meli path already uses `stock_tasks` for pending, ready, synced, and review states. Reusing it keeps operational behavior consistent and prevents duplicate webhook deliveries from applying twice.

Alternative considered: write only `sync_logs` and update Shopify immediately from raw events. That would be simpler but unsafe because duplicate delivery and partial failures would be harder to recover.

### Match Shopify variants by exact SKU

The processor will resolve the Mercado Libre SKU from the order item first, then from the Mercado Libre item/variation fallback when needed. It will then find Shopify variants by exact SKU.

The task can proceed automatically only when exactly one Shopify variant matches.

Rationale: SKU is the shared product identity already confirmed for the project. Exact single-match behavior avoids touching the wrong product.

Alternative considered: match by title, Mercado Libre item id, or local mapping. Title matching is unsafe. Item id mappings can become useful later, but SKU is the current verified identity.

### Apply from observed current Shopify stock

Dry-run will read current Shopify stock for the matched variant and compute:

```text
target_stock = max(current_shopify_stock - quantity_sold, 0)
```

Apply will re-read Shopify stock immediately before writing and compute the target from that fresh value.

Rationale: stock can change between dry-run and apply. Re-reading before apply reduces stale-write risk.

Alternative considered: store dry-run target and apply it later unchanged. This is easier to explain but can overwrite newer stock changes.

### Keep the design compatible with mirror orders later

Task records and logs will store enough Mercado Libre context to support a later Shopify mirror-order flow:

- Mercado Libre order id
- Mercado Libre item id and variation id
- SKU
- quantity sold
- order status
- source payload excerpt

Rationale: this change solves the urgent stock problem while preserving a path to centralized order history.

## Risks / Trade-offs

- Duplicate Mercado Libre notification -> Mitigation: stable task ids with `INSERT OR IGNORE` semantics and synced-state checks before apply.
- Shopify stock changes between dry-run and apply -> Mitigation: apply re-reads current stock before computing the final target.
- Mercado Libre cancellation after stock was decremented -> Mitigation: out of scope for automatic reversal in V1; log canceled orders and add a future cancellation/reconciliation capability.
- SKU missing or duplicated in Shopify -> Mitigation: mark task `needs_review` and do not update stock.
- Shopify variant exists but is not inventory-managed -> Mitigation: mark task `needs_review` or `skipped_no_shopify_inventory` and log the reason.
- Multiple Shopify locations -> Mitigation: use the existing active-location helper for V1 and document the chosen location in logs.
- Direct inventory adjustment means Shopify reports will not show a Shopify order for the Mercado Libre sale -> Mitigation: explicitly defer mirror orders to a later change.

## Migration Plan

1. Extend local schema in a backward-compatible way only by adding nullable columns if needed.
2. Keep existing dry-run behavior working for `--order-id`.
3. Add task creation from Mercado Libre raw events and explicit order ids.
4. Validate with the real order `2000018107143682` in dry-run.
5. Apply a single known order only after dry-run shows one exact Shopify variant match and expected stock.
6. Roll back by stopping the processor; any incorrect applied stock can be corrected manually in Shopify using the audit log.

## Open Questions

- Should the future mirror-order flow be required for accounting/reporting, or is Shopify inventory-only sync enough for now?
- Which Shopify location should be authoritative if more than one active location exists?
- Should canceled Mercado Libre orders automatically restore stock in Shopify, or should cancellation handling stay manual until a reconciliation process exists?
