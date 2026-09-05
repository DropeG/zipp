# Simple Shopify and Mercado Libre Stock Sync

## Goal

Keep Shopify and Mercado Libre stock aligned without building a large or complicated system.

Shopify is the main stock record.

The central rule is:

```text
Mercado Libre orders update Shopify.
Shopify product quantities update Mercado Libre.
```

## Why Orders Are Needed

When a product sells on Mercado Libre, Mercado Libre reduces its own stock. Shopify does not know about that sale.

Example:

```text
Before sale:
Shopify 10
Meli    10

Meli sells 1:
Shopify 10
Meli     9
```

If we copied Shopify to Mercado Libre now, Mercado Libre would incorrectly return to 10. We must first read the Mercado Libre order and reduce Shopify to 9.

After Shopify knows about the sale, it is safe to copy Shopify's quantity to Mercado Libre.

## Simple Architecture

```text
Shopify and Meli webhooks
          |
          v
Verified webhook receiver
          |
          v
      SQLite queue
          |
          v
       One worker
          |
     +----+--------------------+
     |                         |
     v                         v
Meli order                Shopify quantity
reduces Shopify           copied to Meli
```

The system keeps SQLite. It does not need Redis, Kafka, microservices, or another database.

## Part 1: Receive Webhooks Safely

The webhook receiver is the public door used by Shopify and Mercado Libre.

It must:

- reject requests that are too large;
- verify Shopify's signature before trusting the message;
- ignore duplicate webhook IDs;
- check the expected Shopify store and webhook topic;
- store the event before returning success;
- treat Mercado Libre messages only as notices;
- use the Mercado Libre API to fetch and verify the real order.

The receiver only saves work. It does not change stock.

## Part 2: Process Mercado Libre Sales

When Mercado Libre reports an order, the worker:

1. Fetches the real order from Mercado Libre.
2. Confirms that it belongs to the expected seller.
3. Confirms that the order is paid.
4. Reads each product's exact SKU and sold quantity.
5. Creates one unique job for each order line.
6. Reduces Shopify stock by the quantity sold.
7. Records the result.
8. Requests a Shopify-to-Mercado-Libre quantity check for the affected SKU.

The Shopify update must use the atomic `inventoryAdjustQuantities` GraphQL operation.

Example:

```text
Meli order 123 sold SKU-A x 2
Shopify adjustment: -2
```

The unique job ID is also sent to Shopify as the idempotency key. If the same job is retried, Shopify must not apply it twice.

If the order is not paid, the worker does not change stock.

If the SKU is missing or unclear, the job goes to human review.

## Part 3: Process Shopify Sales

Shopify already reduces its own stock when it receives an order.

The worker must not subtract the quantity again. It only schedules a check for each affected SKU:

```text
Read fresh Shopify quantity
        |
        v
Set Mercado Libre to that quantity
```

The quantity must be read immediately before the Mercado Libre update. The worker must never apply an old quantity saved during an earlier dry run.

## Part 4: Combine Repeated Checks

Several sales can happen close together. We do not need a separate Mercado Libre update for every sale.

The queue keeps one pending quantity check per SKU. New requests for the same SKU are combined.

Example:

```text
Three sales affect SKU-A
        |
        v
One fresh Shopify read
        |
        v
One Mercado Libre update
```

If another sale happens while the check is running, the SKU is checked one more time before it becomes idle.

## Part 5: Daily Full Product Check

Once a day, check every inventory-managed product shared between Shopify and Mercado Libre.

The daily job runs in this order:

1. Recover Mercado Libre orders that may have been missed.
2. Apply any missing Mercado Libre sales to Shopify exactly once.
3. Load every Shopify product with a managed SKU.
4. Load every Mercado Libre listing using complete pagination.
5. Match products by exact SKU.
6. Compare their quantities.
7. Update Mercado Libre when its quantity differs from Shopify.
8. Record missing or duplicate SKUs for human review.

The daily product check never changes Shopify. It only makes Mercado Libre match Shopify after missed Mercado Libre orders have been recovered.

## Queue Rules

There is one worker and one clear job flow:

```text
pending
  -> processing
       -> completed
       -> retry later
       -> needs review
```

Before calling an external API, the worker claims the job in SQLite. Another worker cannot claim the same job at the same time.

Each claim has an expiry time. If the worker crashes, the job becomes available again later.

Temporary API and network errors are retried with increasing delays. Permanent problems and repeated failures go to human review.

Jobs always include their type and source. A Shopify job cannot be processed as a Mercado Libre job.

Only one stock operation for the same SKU runs at a time.

## Product Matching Rules

- Match products only by exact SKU.
- Do not guess or use fuzzy matching.
- One exact match can be processed.
- No match is recorded without changing stock.
- More than one match goes to human review.
- Unsupported Mercado Libre variations go to human review.
- Catalog loading must include every page; it must not stop silently after 1,000 listings.

## Dry Run

Dry run remains available for testing and investigation.

It reads current information and shows the proposed action without changing stock. It is not a required step in the normal production queue.

Production jobs validate and apply their action in one run so an old dry-run value cannot be used later.

## Files

The new version will use these main files:

- `automations/stock-sync/scripts/shopify_webhook_catcher.js`: verifies and saves webhooks.
- `automations/stock-sync/scripts/stock_sync_worker.py`: runs and routes jobs.
- `automations/stock-sync/stock_sync/db.py`: manages the queue, claims, retries, and logs.
- `automations/stock-sync/stock_sync/shopify.py`: reads and adjusts Shopify inventory.
- `automations/stock-sync/stock_sync/meli.py`: reads Mercado Libre orders and inventory.
- `automations/stock-sync/stock_sync/handlers.py`: contains the simple rules for each job type.

The two old processors will remain during testing. They will be removed only after the new worker passes the tests and controlled live checks.

## Tests Required

Tests must prove that:

- two Mercado Libre orders for the same SKU both reduce Shopify;
- the same Mercado Libre order cannot reduce Shopify twice;
- several Shopify sales can be combined into one final Mercado Libre update;
- Shopify and Mercado Libre sales can arrive in either order;
- two workers cannot claim the same job;
- a crashed or failed job can be retried safely;
- fresh Shopify stock is used when updating Mercado Libre;
- missing and duplicate SKUs never change stock;
- temporary Mercado Libre API failures are not treated as missing products;
- invalid Shopify webhook signatures are rejected;
- excessively large webhook requests are rejected;
- the daily check reads all product pages.

## Safe Rollout

1. Back up `data/stock_sync.db`.
2. Add the new queue fields without deleting old history.
3. Run automated tests.
4. Run the new worker in dry-run mode.
5. Stop the two old processors.
6. Test one reviewed Mercado Libre order.
7. Test one Shopify-to-Mercado-Libre quantity update.
8. Confirm the quantities and logs on both platforms.
9. Enable the new worker continuously.
10. Enable the daily full product check.

If a problem occurs, stop the worker. Previously confirmed stock changes remain in the audit log and are not automatically reversed.

## Not Included

- Creating Mercado Libre mirror orders in Shopify.
- Customer, payment, shipping, tax, or accounting sync.
- Automatic cancellation or refund stock restoration.
- Advanced multi-location stock allocation.
- Product publishing.
- New distributed infrastructure.

## Done When

- Every paid Mercado Libre order line reduces Shopify no more than once.
- Separate Mercado Libre sales for the same SKU are all counted.
- Shopify sales never reduce Shopify twice.
- Mercado Libre receives a fresh Shopify quantity.
- Duplicate events, temporary failures, overlapping workers, and crashes do not silently lose or duplicate stock changes.
- Missing or unclear SKUs never change stock automatically.
- The daily check examines all shared products and records its result.
