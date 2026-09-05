# Simple Stock Sync Design

## Objective

Replace the current pair of loosely coordinated stock processors with one small, reliable worker while preserving the existing business rules:

- Shopify is the inventory source of truth.
- A paid Mercado Libre sale decreases Shopify inventory exactly once.
- A Shopify sale causes Mercado Libre inventory to converge to Shopify's current quantity.
- Ambiguous or missing SKUs never change inventory automatically.

The design keeps SQLite and the existing webhook server. It does not introduce Redis, Kafka, microservices, or another database.

## Current Problems

The current implementation has several correctness gaps:

- The Shopify-to-Mercado-Libre processor can select Mercado Libre tasks because its queries do not filter by source.
- Shopify-to-Mercado-Libre apply uses stock captured during dry-run instead of reading fresh Shopify stock.
- Two overlapping workers can process the same task or race on the same SKU.
- Rows marked `retryable_error` are not selected again.
- A failed Mercado Libre raw event is given `processed_at`, which prevents retry.
- Mercado Libre-to-Shopify uses a read-subtract-set sequence that can lose an adjustment under concurrency.
- The same database and API concerns are duplicated across two large processors.

## Proposed Architecture

```text
Shopify/Mercado Libre webhook
            |
            v
     authenticated receiver
            |
            v
        SQLite jobs
            |
            v
       one job worker
            |
      +-----+-------------------+
      |                         |
      v                         v
Meli sale job             SKU reconcile job
atomic Shopify -qty       read fresh Shopify stock
with idempotency          set Mercado Libre stock
      |                         |
      +------------+------------+
                   v
             audit and status
```

The webhook receiver only validates and persists work. One worker owns all external inventory mutations and dispatches behavior by explicit job type. This removes cross-processor task selection and makes concurrency rules enforceable in one place.

## Job Types

### `meli_order`

Created from a Mercado Libre order notification. The worker fetches the complete order because the notification does not contain all line details. Only a paid order is expanded into inventory work.

Each valid order line creates a stable `meli_adjustment` job. Missing or ambiguous SKU data moves the relevant job to `needs_review` without changing stock.

### `meli_adjustment`

Represents one paid Mercado Libre order line. Its stable identifier is derived from the Mercado Libre order and line identity.

The worker applies `delta = -quantity_sold` through Shopify GraphQL `inventoryAdjustQuantities`. The job ID is also the Shopify idempotency key. Separate orders therefore accumulate correctly, while retrying the same order line does not deduct twice.

After success, the affected SKU is marked for reconciliation.

### `reconcile_sku`

Represents the request to make one Mercado Libre SKU match Shopify's current available quantity. Shopify order webhooks schedule this job directly. Successful Mercado Libre adjustments also schedule it so cross-channel event ordering converges to the same final inventory.

Reconciliation requests are coalesced by SKU. Several sales occurring close together result in one fresh Shopify read and one Mercado Libre write. If another request arrives while the SKU is being reconciled, the SKU remains dirty and is reconciled once more before becoming idle.

## Queue State Machine

```text
pending
  -> processing
       -> synced
       -> retryable_error -> pending after backoff
       -> needs_review
```

Each job stores:

- stable job ID;
- job type and source;
- order and line identity where applicable;
- SKU and quantity where applicable;
- JSON payload needed for processing and audit;
- status;
- attempt count and next-attempt time;
- processing lease owner and expiration;
- last error;
- creation and update timestamps.

The worker claims one eligible job inside a SQLite `BEGIN IMMEDIATE` transaction. Claiming changes the status to `processing` and assigns a lease before any API call occurs. An expired lease returns to `pending`, allowing recovery after a crash.

Only one `reconcile_sku` operation for a SKU can be active at a time. The initial deployment runs one worker process; the database claim still protects against accidental overlapping scheduler invocations.

## Shopify Inventory Rules

### Mercado Libre sale

The worker does not calculate an absolute Shopify target locally. It submits an atomic negative adjustment:

```text
delta = -quantity_sold
idempotency key = stable meli_adjustment job ID
```

The adjustment response and any user errors are recorded. A successful response completes the adjustment job and marks the SKU for reconciliation.

### Shopify sale

Shopify has already reduced its own inventory. The webhook therefore schedules only `reconcile_sku`; it does not subtract the sale quantity from either platform again.

### Authoritative reconciliation

At execution time, the worker reads fresh Shopify stock for the configured location, resolves exactly one Mercado Libre listing or supported variation by SKU, and sets Mercado Libre to the fresh Shopify quantity. No stock value captured by an earlier dry-run is used for production apply.

## Reconciliation Coalescing

The reconciliation table has one row per SKU with a monotonically increasing requested version and a completed version.

When a sale requests reconciliation, the worker increments the requested version and sets a short future eligibility time. Repeated requests within that interval only advance the version and push the eligibility time; they do not create more rows.

The reconciliation worker captures the requested version when it starts. After applying and confirming the Mercado Libre quantity:

- if the requested version is unchanged, it records that version as completed;
- if a newer request arrived during processing, it leaves the SKU eligible for another pass.

This provides debouncing without losing activity that arrives during an API call.

## Idempotency and Failure Handling

- Webhook identity prevents duplicate incoming events.
- Stable Mercado Libre order-line IDs prevent duplicate adjustment jobs.
- Shopify idempotency keys prevent the same external adjustment from applying twice across retries.
- Retryable network, rate-limit, and server errors use bounded exponential backoff.
- Permanent validation errors move to `needs_review`.
- A configurable maximum attempt count prevents infinite retries and moves exhausted jobs to `needs_review`.
- Success is recorded only after the external API reports success and the expected postcondition is confirmed where the platform permits confirmation.

Webhook events and jobs remain available as an audit trail. Retrying changes status and attempt metadata rather than creating a replacement identity.

## Webhook Safety

- Shopify webhook requests must pass HMAC verification using the raw request body.
- Request bodies have an explicit size limit.
- Mercado Libre notifications are treated as references only; the authenticated API response is the source for order status, seller, SKU, and quantity.
- Notifications and orders for an unexpected seller are rejected or moved to `needs_review`.
- The receiver returns success only after the event is durably stored.

## SKU Resolution

The first implementation keeps exact SKU matching and existing Mercado Libre SKU fallbacks. It does not perform fuzzy matching.

- No match: record a skipped or reviewable outcome without changing stock.
- More than one match: `needs_review`.
- Unsupported Mercado Libre variation: `needs_review` until variation updates are explicitly implemented and tested.

Full-catalog scans must not silently stop at 1,000 listings. The implementation may use complete pagination initially. A persistent SKU mapping or cache can be added later only if measurements show it is necessary.

## Dry Run

Dry-run remains an operator command, not a production queue state. It evaluates a selected job or SKU using current platform data and prints the intended action without changing inventory or moving the production job through `ready_to_apply`.

Normal production processing validates and applies a claimed job in one workflow, avoiding stale values between two separately scheduled commands.

## Files and Boundaries

The implementation will retain focused entry points while extracting shared responsibilities:

- `automations/stock-sync/scripts/shopify_webhook_catcher.js`: authenticate and persist webhook events only.
- `automations/stock-sync/scripts/stock_sync_worker.py`: claim and dispatch jobs.
- `automations/stock-sync/stock_sync/db.py`: schema, migrations, atomic claiming, leases, retry state, and reconciliation versions.
- `automations/stock-sync/stock_sync/shopify.py`: Shopify GraphQL inventory reads and idempotent adjustments.
- `automations/stock-sync/stock_sync/meli.py`: Mercado Libre order lookup, SKU resolution, inventory reads, and writes.
- `automations/stock-sync/stock_sync/handlers.py`: the three job handlers and their business rules.

The existing processors remain available only during migration and are removed after equivalent tests and a controlled dry-run demonstrate the new worker behavior.

## Testing

Automated tests must cover:

- two Mercado Libre orders for the same SKU before processing;
- duplicate delivery of the same Mercado Libre order;
- two Shopify orders coalescing into one SKU reconciliation;
- a Shopify and Mercado Libre sale arriving in either order;
- an overlapping worker failing to claim an already leased job;
- a crash after Shopify receives an adjustment and safe retry with the same idempotency key;
- retryable event and task recovery;
- a new reconciliation request arriving while reconciliation is processing;
- fresh Shopify stock being read immediately before Mercado Libre apply;
- missing SKU, duplicate SKU, unsupported variation, and absent listing behavior;
- partial Mercado Libre API failures not being interpreted as missing products;
- webhook HMAC rejection and request-size rejection.

The existing ten Mercado Libre-to-Shopify tests remain as regression coverage until equivalent tests exist for the new worker.

## Deployment and Migration

1. Back up `data/stock_sync.db`.
2. Apply additive schema migrations; do not discard historical events, tasks, or logs.
3. Run the new worker in dry-run against selected historical fixtures and current API reads.
4. Stop both legacy processors before enabling mutation mode in the new worker.
5. Apply one reviewed Mercado Libre adjustment and one Shopify reconciliation.
6. Confirm both platform quantities and the audit records.
7. Enable the single worker continuously.
8. Keep a periodic full Shopify-to-Mercado-Libre reconciliation as a later operational safety job.

Rollback consists of stopping the new worker and restoring operation with no automatic apply. External inventory mutations already confirmed before rollback remain auditable and are not automatically reversed.

## Non-Goals

- Creating mirror orders in Shopify.
- Synchronizing customer, payment, shipping, fulfillment, tax, or accounting data.
- Automatically restoring inventory for cancellations and refunds.
- Advanced multi-location allocation.
- Replacing SQLite or deploying distributed infrastructure.
- Publishing new Mercado Libre products.

## Acceptance Criteria

- Each paid Mercado Libre order line decreases Shopify inventory no more than once.
- Distinct Mercado Libre order lines for the same SKU all contribute their quantity without lost updates.
- Shopify-originated sales never cause an additional Shopify decrement.
- Mercado Libre converges to a fresh Shopify quantity regardless of event arrival order.
- Duplicate events, worker overlap, temporary API failures, and worker crashes do not silently lose or duplicate inventory changes.
- No worker can process a job owned by the opposite source or an unclaimed job.
- Missing or ambiguous product identity never changes inventory automatically.
- Production processing no longer depends on a stored dry-run stock value.
