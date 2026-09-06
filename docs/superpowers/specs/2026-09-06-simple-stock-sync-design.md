# Simple Shopify and Mercado Libre Stock Sync

## Goal

Keep every sale and the final stock in Shopify.

Shopify is the main place for orders and inventory.

The central rule is:

```text
Mercado Libre paid order -> create a tagged Shopify order
Shopify stock            -> copy the final quantity to Mercado Libre
```

The first version imports only the information needed for order tracking and stock:

- Shopify product variant;
- quantity sold;
- Mercado Libre unit price and currency;
- paid status;
- `mercadolibre` tag;
- unique Mercado Libre order ID.

It does not import customer, address, shipping, tax, discount, or detailed payment information yet.

## Why Create a Shopify Order

When Mercado Libre sells a product, Mercado Libre reduces its own stock. Shopify does not know about the sale.

Creating the same order in Shopify gives us one central order list and lets Shopify reduce its stock naturally.

Example:

```text
Before sale:
Shopify 10
Meli    10

Meli sells 1:
Shopify 10
Meli     9

Create tagged Shopify order:
Shopify  9
Meli     9
```

We do not need a separate direct Shopify inventory adjustment for that Mercado Libre sale.

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
     +----+-------------------+
     |                        |
     v                        v
Create Shopify order     Copy fresh Shopify
from Meli order          quantity to Meli
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

The receiver only saves work. It does not create orders or change stock.

## Part 2: Import Mercado Libre Orders into Shopify

When Mercado Libre reports an order, the worker:

1. Fetches the real order from Mercado Libre.
2. Confirms that it belongs to the expected seller.
3. Confirms that the order is paid.
4. Reads each product's exact SKU and quantity.
5. Finds exactly one Shopify variant for every SKU.
6. Checks that this Mercado Libre order was not imported before.
7. Creates one paid Shopify order containing all valid order lines.
8. Uses the unit price and currency reported by Mercado Libre for each line.
9. Adds the `mercadolibre` tag.
10. Adds a unique tag such as `meli-order-2000018107143682`.
11. Saves the Mercado Libre order ID as Shopify's external source ID.
12. Tells Shopify to reduce inventory for the order.
13. Saves the new Shopify order ID in SQLite.
14. Requests a Mercado Libre quantity check for every affected SKU.

The imported Shopify order does not send an order or fulfillment email.

The order uses Shopify's `orderCreate` GraphQL operation with inventory decrement enabled. Because the sale already happened on Mercado Libre, the order must still be recorded if Shopify's inventory policy would normally block it. Any resulting stock shortage is recorded for human review.

## Duplicate Protection

A repeated Mercado Libre webhook must not create another Shopify order.

Before creating an order, the worker checks:

1. The local SQLite link between Mercado Libre and Shopify order IDs.
2. Shopify for the unique `meli-order-<id>` tag or external source ID.

If the Shopify order already exists, the worker records the link locally and completes the job without creating another order.

This second check protects against this failure:

```text
Shopify order created
        |
worker crashes before saving Shopify order ID
        |
job retries
```

## Part 3: Process Shopify Order Webhooks

Shopify already reduces inventory when it creates a normal Shopify order or an imported Mercado Libre order.

The worker must never subtract the quantity again.

For each affected SKU, it only requests a quantity check:

```text
Read fresh Shopify quantity
        |
        v
Set Mercado Libre to that quantity
```

Each order checks every affected SKU once. If another order for the same SKU arrives later, the worker checks that SKU again using fresh Shopify stock.

The `mercadolibre` tag identifies imported orders in Shopify and makes their origin visible. Receiving Shopify's webhook for an imported order does not create another Shopify order and does not cause another inventory deduction.

The quantity must be read immediately before the Mercado Libre update. The worker must never apply an old quantity saved during a dry run.

## Part 4: Daily Full Product Check

Once a day, check every inventory-managed product shared between Shopify and Mercado Libre.

The daily job runs in this order:

1. Find Mercado Libre orders that may have been missed.
2. Import any missing paid orders into Shopify exactly once.
3. Load every Shopify product with a managed SKU.
4. Load every Mercado Libre listing using complete pagination.
5. Match products by exact SKU.
6. Compare their quantities.
7. Update Mercado Libre when its quantity differs from Shopify.
8. Record missing or duplicate SKUs for human review.

The daily product check never changes Shopify directly. Shopify changes only through normal Shopify orders and imported Mercado Libre orders.

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

Human review happens in Shopify. The worker creates or updates a review draft when there is no real Shopify order to mark. The draft order rules are described below.

Jobs always include their type and source. A Shopify job cannot be processed as a Mercado Libre import job.

Only one order import for the same Mercado Libre order and one quantity update for the same SKU can run at a time.

## Product Matching Rules

- Match products only by exact SKU.
- Do not guess or use fuzzy matching.
- Every Mercado Libre order line must match exactly one Shopify variant before the order is created.
- If one line cannot be matched safely, the whole order goes to human review and no partial Shopify order is created.
- More than one Shopify match for a SKU goes to human review.
- More than one Mercado Libre listing for a SKU goes to human review.
- Unsupported Mercado Libre variations go to human review.
- Catalog loading must include every page; it must not stop silently after 1,000 listings.

## Human Review in Shopify

Shopify draft orders are the review inbox. The worker does not create an empty or fake paid order.

If a Mercado Libre order cannot be imported before the real Shopify order exists, the worker creates one draft order containing:

- one $0 custom item named `Mercado Libre order needs review`;
- the `mercadolibre` and `meli-needs-review` tags;
- a unique tag such as `meli-review-2000018107143682`;
- the Mercado Libre order ID;
- the SKU, quantity, and product title when available;
- a simple explanation of the problem;
- the number of attempts and the last error.

The review draft does not reserve or reduce inventory, does not count as a paid sale, and does not send an email to the customer.

Retries must update the existing review draft instead of creating another one. The worker checks SQLite and Shopify using the Mercado Libre order ID before creating a review draft.

For a product problem that is not connected to one order, such as a missing or duplicate SKU found by the daily check, the worker creates one review draft for that SKU and problem type. Later checks update the same draft. When a later check confirms that the problem is gone, it replaces `meli-needs-review` with `meli-review-resolved`.

To resolve an order problem, a person fixes the product or SKU in Shopify and retries the job. The worker then creates the correct paid order, removes `meli-needs-review` from the draft, adds `meli-review-resolved`, and adds the real Shopify order ID to the draft note. The draft remains as a simple history of what happened.

If the problem happens after the real Shopify order already exists, the worker does not create a draft. It adds `meli-needs-review` and the error details to the real order. After resolution, it removes that tag and adds `meli-review-resolved`.

## Order Data in the First Version

The imported Shopify order contains:

- matched Shopify variants;
- quantities sold;
- Mercado Libre unit price and currency for each line;
- paid financial status;
- `mercadolibre` tag;
- unique `meli-order-<id>` tag;
- Mercado Libre order ID as the external source ID;
- a short note saying it was imported from Mercado Libre.

The first version deliberately excludes:

- customer name, email, and phone;
- shipping and billing addresses;
- shipping method and tracking;
- taxes and tax lines;
- discounts;
- detailed payment transaction data.

The merchandise subtotal can be shown using Mercado Libre line prices, but shipping, tax, discounts, fees, and payout details are not reproduced. The first version is suitable for finding orders and tracking stock in Shopify. It is not yet a complete accounting, customer-service, or fulfillment copy of Mercado Libre.

## Dry Run

Dry run remains available for testing and investigation.

For a Mercado Libre order, it shows the Shopify variants, quantities, tags, and inventory behavior that would be used without creating the order.

For a quantity check, it shows the current Shopify and Mercado Libre quantities without changing them.

Dry run is not a required production queue state. Production work validates and applies in one run so old values are not used later.

## Files

The new version will use these main files:

- `automations/stock-sync/scripts/shopify_webhook_catcher.js`: verifies and saves webhooks.
- `automations/stock-sync/scripts/stock_sync_worker.py`: runs and routes jobs.
- `automations/stock-sync/stock_sync/db.py`: manages the queue, claims, retries, order links, and logs.
- `automations/stock-sync/stock_sync/shopify.py`: finds variants, creates imported orders, reads inventory, and manages review drafts.
- `automations/stock-sync/stock_sync/meli.py`: reads Mercado Libre orders and inventory.
- `automations/stock-sync/stock_sync/handlers.py`: contains the rules for importing orders and updating quantities.

The two old processors remain during testing. They are removed only after the new worker passes the tests and controlled live checks.

## Tests Required

Tests must prove that:

- a paid Mercado Libre order creates one paid, tagged Shopify order;
- the Shopify order contains the correct variants, quantities, unit prices, and currency;
- creating the Shopify order reduces inventory once;
- the same Mercado Libre order cannot create two Shopify orders;
- retry after a crash finds the Shopify order that was already created;
- unpaid Mercado Libre orders do not create Shopify orders;
- one unsafe line prevents a partial Shopify order;
- an unsafe Mercado Libre order creates one review draft with the correct tags and details;
- retrying the same problem updates the existing draft instead of creating another draft;
- a review draft never changes inventory or sends a customer email;
- resolving the problem creates the correct real order and marks the review draft as resolved;
- a problem found after the real order exists marks that order for review instead of creating a draft;
- a daily product problem creates or updates one review draft for that SKU and problem type;
- Shopify webhooks for imported orders do not reduce inventory again;
- two Shopify orders for the same SKU are processed sequentially using fresh stock;
- Shopify and Mercado Libre sales can arrive in either order;
- two workers cannot claim the same job;
- failed jobs can be retried safely;
- fresh Shopify stock is used when updating Mercado Libre;
- missing and duplicate SKUs never change stock;
- temporary Mercado Libre API failures are not treated as missing products;
- invalid Shopify webhook signatures are rejected;
- excessively large webhook requests are rejected;
- the daily check reads all product pages;
- the daily check imports missing Mercado Libre orders before copying product quantities.

## Safe Rollout

1. Back up `data/stock_sync.db`.
2. Add the new queue and order-link fields without deleting old history.
3. Run automated tests.
4. Run the new worker in dry-run mode.
5. Stop the two old processors.
6. Import one reviewed Mercado Libre order into Shopify.
7. Confirm the order, tags, inventory change, and local order link.
8. Test one Shopify-to-Mercado-Libre quantity update.
9. Confirm the quantities and logs on both platforms.
10. Enable the new worker continuously.
11. Enable the daily full product check.

If a problem occurs, stop the worker. Imported Shopify orders and confirmed stock changes are not deleted or automatically reversed.

## Not Included

- Customer, address, shipping, tax, discount, and detailed payment import.
- Automatic cancellation or refund updates between platforms.
- Advanced multi-location stock allocation.
- Product publishing.
- New distributed infrastructure.

## Done When

- Every paid Mercado Libre order creates one tagged Shopify order.
- The same Mercado Libre order can never create two Shopify orders.
- Each imported order reduces Shopify inventory once.
- Shopify order webhooks never reduce Shopify inventory again.
- Mercado Libre receives a fresh Shopify quantity.
- Duplicate events, temporary failures, overlapping workers, and crashes do not silently lose or duplicate orders or stock changes.
- Missing or unclear SKUs never create a partial order or change stock automatically.
- Problems that need attention appear in Shopify as one review draft, or on the real order when it already exists.
- The daily check imports missed Mercado Libre orders before checking all shared products.
