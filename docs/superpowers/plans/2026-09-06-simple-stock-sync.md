# Simple Shopify and Mercado Libre Stock Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one production service that imports paid Mercado Libre orders into Shopify, treats Shopify inventory as authoritative, copies fresh quantities back to Mercado Libre, and surfaces problems in Shopify.

**Architecture:** A small Node receiver verifies and saves webhook events in SQLite, then a single Python worker claims and handles the resulting jobs. Paid Mercado Libre orders become tagged Shopify orders; all stock reconciliation reads Shopify immediately before setting Mercado Libre. A daily job recovers missed orders and checks all shared SKUs.

**Tech Stack:** Node.js 22+ built-ins (`http`, `crypto`, `node:sqlite`), Python 3.11+, SQLite, `requests`, `pytest`, Shopify Admin GraphQL API 2026-07, Mercado Libre REST API.

**Spec:** `docs/superpowers/specs/2026-09-06-simple-stock-sync-design.md`

## Global Constraints

- All new runtime code, tests, dependencies, and run instructions live under `prod/stock-sync/`.
- Shopify is the source of truth for inventory.
- Match products only by exact, non-empty SKU; never guess or fuzzy-match.
- Import a Mercado Libre order only after the API confirms the expected seller and `paid` status.
- Import each Mercado Libre order exactly once, including after a crash between Shopify creation and the local database write.
- Use Shopify `orderCreate` with `inventoryBehaviour: DECREMENT_IGNORING_POLICY`, `sendReceipt: false`, and `sendFulfillmentReceipt: false`.
- Imported orders use Mercado Libre unit prices and currency and contain no customer, address, shipping, tax, discount, or detailed payment data.
- Review drafts use one $0 custom item and never reserve inventory or send email.
- Process jobs with one worker; every reconciliation reads fresh Shopify inventory immediately before updating Mercado Libre.
- The daily run recovers missed paid orders before checking all shared products.
- Keep the old processors unchanged until automated tests and controlled live checks pass.

## File Map

- `prod/stock-sync/schema.sql`: the only SQLite schema definition, used by Node and Python.
- `prod/stock-sync/receiver.js`: authenticated HTTP endpoints that save events and enqueue jobs.
- `prod/stock-sync/worker.py`: CLI entry point for migrations, queued work, dry runs, retries, and the daily run.
- `prod/stock-sync/stock_sync/config.py`: validates environment configuration and filesystem paths.
- `prod/stock-sync/stock_sync/db.py`: transactions, enqueueing, claims, retries, links, review records, logs, and checkpoints.
- `prod/stock-sync/stock_sync/models.py`: small immutable records passed between clients and handlers.
- `prod/stock-sync/stock_sync/errors.py`: retry-versus-review error types.
- `prod/stock-sync/stock_sync/shopify.py`: Shopify GraphQL calls and payload construction.
- `prod/stock-sync/stock_sync/meli.py`: Mercado Libre authentication, orders, listings, and quantity updates.
- `prod/stock-sync/stock_sync/handlers.py`: order import and per-SKU reconciliation rules.
- `prod/stock-sync/stock_sync/daily.py`: missed-order recovery followed by complete product reconciliation.
- `prod/stock-sync/tests/`: isolated tests using temporary SQLite databases and fake API clients.
- `prod/stock-sync/README.md`: setup, required scopes, commands, deployment, review, and rollback instructions.

---

### Task 1: Production Foundation and Durable Queue

**Files:**
- Create: `prod/stock-sync/requirements.txt`
- Create: `prod/stock-sync/package.json`
- Create: `prod/stock-sync/.env.example`
- Create: `prod/stock-sync/schema.sql`
- Create: `prod/stock-sync/stock_sync/__init__.py`
- Create: `prod/stock-sync/stock_sync/config.py`
- Create: `prod/stock-sync/stock_sync/models.py`
- Create: `prod/stock-sync/stock_sync/errors.py`
- Create: `prod/stock-sync/stock_sync/db.py`
- Create: `prod/stock-sync/tests/conftest.py`
- Create: `prod/stock-sync/tests/test_db.py`

**Interfaces:**
- Produces: `Settings.from_env() -> Settings`.
- Produces: `Database(path)`, `enqueue_job()`, `claim_next_job()`, `complete_job()`, `retry_job()`, `needs_review()`, `link_order()`, `link_review()`, `get_checkpoint()`, and `set_checkpoint()`.
- Produces: `Job`, `MeliOrder`, `MeliOrderLine`, `ShopifyVariant`, `MeliListing`, `RetryableSyncError`, and `ReviewRequiredError`.

- [ ] **Step 1: Write queue and configuration tests**

```python
def test_duplicate_source_key_creates_one_job(db):
    first = db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")
    second = db.enqueue_job("import_meli_order", "meli:123", {"order_id": "123"}, "order:123")
    assert first == second
    assert db.count("jobs") == 1

def test_claim_is_atomic_and_respects_resource_lock(db, clock):
    db.enqueue_job("reconcile_sku", "order:1:ABC", {"sku": "ABC"}, "sku:ABC")
    db.enqueue_job("reconcile_sku", "order:2:ABC", {"sku": "ABC"}, "sku:ABC")
    first = db.claim_next_job(clock.now())
    assert first.resource_key == "sku:ABC"
    assert db.claim_next_job(clock.now()) is None

def test_retry_makes_job_available_at_requested_time(db, clock):
    job = db.seed_claimed_job()
    db.retry_job(job.id, "timeout", clock.now(), delay_seconds=300)
    assert db.get_job(job.id).status == "retry_wait"
    assert db.get_job(job.id).available_at == "2026-09-06T00:05:00+00:00"
```

- [ ] **Step 2: Run the tests and confirm they fail because the package does not exist**

Run: `python -m pytest prod/stock-sync/tests/test_db.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'stock_sync'`.

- [ ] **Step 3: Add the schema and focused data types**

Use these tables in `schema.sql`: `events`, `jobs`, `order_links`, `review_links`, `sync_logs`, and `checkpoints`. Give `events` a unique `(source, event_key)`, `jobs` a unique `(job_type, source_key)`, and `order_links.meli_order_id` a primary key. Jobs contain `status`, `attempts`, `available_at`, `lease_until`, `resource_key`, and `last_error`; allowed states are `pending`, `processing`, `completed`, `retry_wait`, and `needs_review`.

```python
@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    source_key: str
    resource_key: str
    payload: dict[str, Any]
    status: str
    attempts: int

class RetryableSyncError(RuntimeError):
    pass

class ReviewRequiredError(RuntimeError):
    def __init__(self, review_key: str, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.review_key = review_key
        self.details = details
```

- [ ] **Step 4: Implement `Settings` and `Database` minimally**

`Settings.from_env()` must require `SHOPIFY_SHOP_URL`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_WEBHOOK_SECRET`, `MELI_APP_ID`, `MELI_CLIENT_SECRET`, `MELI_EXPECTED_SELLER_ID`, and `MELI_WEBHOOK_TOKEN`. Default `SHOPIFY_API_VERSION` to `2026-07`, `MAX_WEBHOOK_BYTES` to `1048576`, and the database to `prod/stock-sync/data/stock_sync.db`.

Use `BEGIN IMMEDIATE` in `claim_next_job()`. Select one due job whose `resource_key` has no unexpired `processing` job, change it to `processing`, increment `attempts`, set a five-minute lease, and return it in the same transaction. Expired leases become `retry_wait` before selection.

- [ ] **Step 5: Run foundation tests**

Run: `python -m pytest prod/stock-sync/tests/test_db.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the foundation**

```bash
git add prod/stock-sync
git commit -m "feat: add stock sync queue foundation"
```

---

### Task 2: Authenticated Webhook Receiver

**Files:**
- Create: `prod/stock-sync/receiver.js`
- Create: `prod/stock-sync/tests/test_receiver.js`
- Modify: `prod/stock-sync/package.json`

**Interfaces:**
- Consumes: `schema.sql` and the `events`/`jobs` uniqueness rules from Task 1.
- Produces: `POST /webhooks/shopify/orders-create`, `POST /webhooks/meli`, and `GET /health`.

- [ ] **Step 1: Write receiver tests with a temporary database and random local port**

```javascript
test("rejects an invalid Shopify HMAC without saving an event", async () => {
  const response = await postShopify("{}", {"x-shopify-hmac-sha256": "bad"});
  assert.equal(response.status, 401);
  assert.equal(countRows(dbPath, "events"), 0);
});

test("deduplicates Shopify delivery IDs", async () => {
  await postSignedShopify(orderPayload, "delivery-1");
  await postSignedShopify(orderPayload, "delivery-1");
  assert.equal(countRows(dbPath, "events"), 1);
  assert.equal(countRows(dbPath, "jobs"), 1);
});

test("rejects oversized bodies before JSON parsing", async () => {
  const response = await postRaw("/webhooks/meli", "x".repeat(1025), {"x-webhook-token": "secret"});
  assert.equal(response.status, 413);
});
```

- [ ] **Step 2: Run Node tests and confirm the receiver is missing**

Run: `cd prod/stock-sync && node --test tests/test_receiver.js`

Expected: FAIL because `receiver.js` does not exist.

- [ ] **Step 3: Implement the receiver with built-in modules only**

For Shopify, calculate base64 HMAC-SHA256 over the raw body and compare with `crypto.timingSafeEqual`. Require `orders/create`, the configured `myshopify.com` domain, and `X-Shopify-Webhook-Id`. For Mercado Libre, compare `X-Webhook-Token` with `MELI_WEBHOOK_TOKEN`, accept only resources matching `/orders/<digits>`, and treat the body only as a notice.

Insert the event and job in one SQLite transaction. Use Shopify order ID as `source_key=shopify-order:<id>` and Mercado Libre order ID as `source_key=meli-order:<id>`. Return `200` for duplicates and valid saved deliveries, `400` for malformed input, `401` for failed authentication, `413` for oversized input, and `503` when SQLite cannot durably save the event.

- [ ] **Step 4: Run receiver tests**

Run: `cd prod/stock-sync && node --test tests/test_receiver.js`

Expected: PASS.

- [ ] **Step 5: Commit the receiver**

```bash
git add prod/stock-sync/receiver.js prod/stock-sync/tests/test_receiver.js prod/stock-sync/package.json
git commit -m "feat: add authenticated stock sync receiver"
```

---

### Task 3: Shopify Client

**Files:**
- Create: `prod/stock-sync/stock_sync/shopify.py`
- Create: `prod/stock-sync/tests/test_shopify.py`

**Interfaces:**
- Produces: `ShopifyClient.find_variants_by_skus(skus) -> dict[str, list[ShopifyVariant]]`.
- Produces: `find_imported_order(meli_order_id) -> str | None`, `create_imported_order(order, variants) -> str`, `get_available_quantity(sku) -> int`, `create_or_update_review(...) -> str`, `resolve_review(...)`, and `mark_order_review(...)`.

- [ ] **Step 1: Write payload, pagination, and API-error tests**

```python
def test_create_imported_order_uses_meli_prices_and_safe_options(shopify, transport):
    order_id = shopify.create_imported_order(meli_order(), {"ABC": variant("ABC")})
    variables = transport.last_variables
    assert order_id == "gid://shopify/Order/77"
    assert variables["order"]["financialStatus"] == "PAID"
    assert variables["order"]["sourceIdentifier"] == "2001"
    assert variables["order"]["lineItems"][0]["variantId"].endswith("/11")
    assert variables["order"]["lineItems"][0]["priceSet"]["shopMoney"]["amount"] == "12990"
    assert variables["options"] == {
        "inventoryBehaviour": "DECREMENT_IGNORING_POLICY",
        "sendReceipt": False,
        "sendFulfillmentReceipt": False,
    }

def test_duplicate_sku_returns_both_variants(shopify, transport):
    transport.pages = [variant_page("ABC", 11, has_next=True), variant_page("ABC", 22)]
    assert len(shopify.find_variants_by_skus({"ABC"})["ABC"]) == 2

def test_user_errors_raise_review_error(shopify, transport):
    transport.reply_with_user_error("lineItems", "Variant is invalid")
    with pytest.raises(ReviewRequiredError):
        shopify.create_imported_order(meli_order(), {"ABC": variant("ABC")})
```

- [ ] **Step 2: Run tests and confirm the client is missing**

Run: `python -m pytest prod/stock-sync/tests/test_shopify.py -q`

Expected: FAIL with `ModuleNotFoundError` for `stock_sync.shopify`.

- [ ] **Step 3: Implement one GraphQL transport and paginated queries**

Use `POST https://<shop>/admin/api/2026-07/graphql.json`. Network failures, HTTP 429, and HTTP 5xx raise `RetryableSyncError`; invalid credentials and GraphQL `userErrors` raise `ReviewRequiredError` with operation and fields. Query all variant pages and preserve duplicate SKU matches instead of overwriting them.

- [ ] **Step 4: Implement imported orders and idempotency lookup**

Create line items with `variantId`, `quantity`, and a `priceSet.shopMoney` using the Mercado Libre unit price and currency. Set `processedAt` from the Mercado Libre order, `financialStatus: PAID`, tags `mercadolibre` and `meli-order-<id>`, `sourceIdentifier`, and a short note. Search Shopify for `meli-order-<id>` before creation. Do not send receipts.

- [ ] **Step 5: Implement review drafts and order review tags**

Create review drafts with one custom line `{title: "Mercado Libre order needs review", quantity: 1, originalUnitPrice: "0"}`, the review note, and tags `mercadolibre`, `meli-needs-review`, and the stable review tag. Search by stable tag before creating; update the found draft. Resolution replaces `meli-needs-review` with `meli-review-resolved`. Existing real orders use `orderUpdate` instead of a new draft.

- [ ] **Step 6: Run Shopify tests**

Run: `python -m pytest prod/stock-sync/tests/test_shopify.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the Shopify client**

```bash
git add prod/stock-sync/stock_sync/shopify.py prod/stock-sync/tests/test_shopify.py
git commit -m "feat: add Shopify order and review client"
```

---

### Task 4: Mercado Libre Client

**Files:**
- Create: `prod/stock-sync/stock_sync/meli.py`
- Create: `prod/stock-sync/tests/test_meli.py`

**Interfaces:**
- Produces: `MeliClient.get_order(order_id) -> MeliOrder`, `verify_seller()`, `search_paid_orders(since)`, `list_all_listings() -> list[MeliListing]`, and `set_available_quantity(listing, quantity)`.

- [ ] **Step 1: Write authentication, order, SKU, pagination, and update tests**

```python
def test_get_order_rejects_wrong_seller(meli, transport):
    transport.json = paid_order_json(seller_id=999)
    with pytest.raises(ReviewRequiredError, match="seller"):
        meli.get_order("2001")

def test_variation_sku_is_loaded_from_item_when_order_line_lacks_it(meli, transport):
    transport.queue(paid_order_json(sku=None, variation_id=8), item_json(variation_id=8, sku="ABC"))
    assert meli.get_order("2001").lines[0].sku == "ABC"

def test_list_all_listings_uses_every_page(meli, transport):
    transport.queue(search_page(["MLC1"], total=2), search_page(["MLC2"], total=2), item_json("MLC1"), item_json("MLC2"))
    assert [item.item_id for item in meli.list_all_listings()] == ["MLC1", "MLC2"]
```

- [ ] **Step 2: Run tests and confirm the client is missing**

Run: `python -m pytest prod/stock-sync/tests/test_meli.py -q`

Expected: FAIL with `ModuleNotFoundError` for `stock_sync.meli`.

- [ ] **Step 3: Implement token loading and refresh**

Read `MELI_TOKENS_FILE`; refresh two minutes before expiry using `/oauth/token`; write tokens atomically through a temporary file and rename. Treat 429 and 5xx as retryable, 401 after one refresh as review, and malformed successful payloads as review.

- [ ] **Step 4: Implement verified order parsing**

Fetch `/orders/{id}`, require the configured seller and `paid` status, and return exact order lines with item ID, variation ID, title, quantity, unit price, currency, and SKU. Use `seller_custom_field` first; when absent, fetch the item and read the matching variation's `SELLER_SKU`. Leave the SKU empty when it cannot be proven so the handler can create a review.

- [ ] **Step 5: Implement full listing and paid-order pagination**

Use seller search endpoints until `offset + results >= total`. Fetch item details needed for exact item/variation SKU mapping. `set_available_quantity()` updates the variation when a variation ID exists; otherwise it updates the item. Clamp only negative Shopify quantities to zero and confirm the returned quantity.

- [ ] **Step 6: Run Mercado Libre tests**

Run: `python -m pytest prod/stock-sync/tests/test_meli.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the Mercado Libre client**

```bash
git add prod/stock-sync/stock_sync/meli.py prod/stock-sync/tests/test_meli.py
git commit -m "feat: add verified Mercado Libre client"
```

---

### Task 5: Import Paid Mercado Libre Orders

**Files:**
- Create: `prod/stock-sync/stock_sync/handlers.py`
- Create: `prod/stock-sync/tests/test_import_handler.py`

**Interfaces:**
- Consumes: `Database`, `ShopifyClient`, and `MeliClient`.
- Produces: `handle_import_meli_order(job, db, shopify, meli, dry_run=False) -> dict`.

- [ ] **Step 1: Write end-to-end handler tests with fake clients**

```python
def test_paid_order_creates_one_shopify_order_and_reconcile_jobs(ctx):
    ctx.meli.order = meli_order(lines=[line("ABC", 2), line("XYZ", 1)])
    ctx.shopify.matches = {"ABC": [variant("ABC")], "XYZ": [variant("XYZ")]}
    handle_import_meli_order(ctx.job, ctx.db, ctx.shopify, ctx.meli)
    assert ctx.shopify.created_orders == ["2001"]
    assert ctx.db.order_link("2001").shopify_order_id == "gid://shopify/Order/77"
    assert ctx.db.job_source_keys("reconcile_sku") == {"shopify-order:77:ABC", "shopify-order:77:XYZ"}

def test_one_unsafe_line_prevents_partial_order_and_creates_review(ctx):
    ctx.meli.order = meli_order(lines=[line("ABC", 1), line("", 1)])
    handle_import_meli_order(ctx.job, ctx.db, ctx.shopify, ctx.meli)
    assert ctx.shopify.created_orders == []
    assert ctx.shopify.review_calls[0].tags == {"mercadolibre", "meli-needs-review", "meli-review-2001"}

def test_crash_recovery_finds_existing_shopify_order(ctx):
    ctx.shopify.existing_order_id = "gid://shopify/Order/77"
    handle_import_meli_order(ctx.job, ctx.db, ctx.shopify, ctx.meli)
    assert ctx.db.order_link("2001").shopify_order_id.endswith("/77")
    assert ctx.shopify.created_orders == []
```

- [ ] **Step 2: Run tests and confirm handler functions are missing**

Run: `python -m pytest prod/stock-sync/tests/test_import_handler.py -q`

Expected: FAIL importing `handle_import_meli_order`.

- [ ] **Step 3: Implement validation before mutation**

Fetch the trusted order, return successfully without an order for non-paid status, require at least one line, require every quantity to be positive, and require exactly one managed Shopify variant for every exact SKU. Collect every problem before creating anything. For a permanent validation problem, create or update one `meli-review-<order_id>` draft and mark the job `needs_review`.

- [ ] **Step 4: Implement exactly-once Shopify order creation**

Check `order_links`, then Shopify by source identifier/tag, and only then call `create_imported_order`. Save the returned link. Enqueue one `reconcile_sku` job per distinct SKU using the returned Shopify order ID, so a direct enqueue and the later Shopify webhook deduplicate to the same source key.

- [ ] **Step 5: Implement dry run**

With `dry_run=True`, return the resolved variants, quantities, prices, currency, tags, and inventory behavior without creating an order, draft, link, or reconciliation job.

- [ ] **Step 6: Run import tests**

Run: `python -m pytest prod/stock-sync/tests/test_import_handler.py -q`

Expected: PASS.

- [ ] **Step 7: Commit order import**

```bash
git add prod/stock-sync/stock_sync/handlers.py prod/stock-sync/tests/test_import_handler.py
git commit -m "feat: import Mercado Libre orders into Shopify"
```

---

### Task 6: Shopify Order Events and Fresh Quantity Reconciliation

**Files:**
- Modify: `prod/stock-sync/stock_sync/handlers.py`
- Create: `prod/stock-sync/tests/test_reconcile_handler.py`

**Interfaces:**
- Produces: `handle_shopify_order(job, db) -> None` and `handle_reconcile_sku(job, db, shopify, meli, dry_run=False) -> dict`.

- [ ] **Step 1: Write order-event and race-condition tests**

```python
def test_shopify_order_only_enqueues_sku_checks(ctx):
    handle_shopify_order(shopify_order_job(lines=[{"sku": "ABC"}, {"sku": "ABC"}]), ctx.db)
    assert ctx.db.job_source_keys("reconcile_sku") == {"shopify-order:88:ABC"}
    assert ctx.shopify.inventory_adjustments == []

def test_two_orders_read_fresh_stock_sequentially(ctx):
    ctx.shopify.quantity_reads = [8, 7]
    handle_reconcile_sku(reconcile_job("order:1:ABC"), ctx.db, ctx.shopify, ctx.meli)
    handle_reconcile_sku(reconcile_job("order:2:ABC"), ctx.db, ctx.shopify, ctx.meli)
    assert ctx.meli.quantity_writes == [("ABC", 8), ("ABC", 7)]

def test_meli_and_shopify_sales_in_either_order_use_final_shopify_quantity(ctx):
    ctx.shopify.quantity_reads = [8]
    handle_reconcile_sku(reconcile_job("order:2:ABC"), ctx.db, ctx.shopify, ctx.meli)
    assert ctx.meli.quantity_writes[-1] == ("ABC", 8)
```

- [ ] **Step 2: Run tests and confirm reconciliation functions are missing**

Run: `python -m pytest prod/stock-sync/tests/test_reconcile_handler.py -q`

Expected: FAIL importing the new handler functions.

- [ ] **Step 3: Implement Shopify order routing without inventory subtraction**

Read distinct non-empty SKUs from the stored Shopify order payload and enqueue one `reconcile_sku` job per SKU. Missing SKU lines create or update a review on the real Shopify order. Never call a Shopify inventory mutation from this handler.

- [ ] **Step 4: Implement fresh reconciliation**

Immediately before the Mercado Libre update, call `shopify.get_available_quantity(sku)`. Resolve exactly one Mercado Libre item or variation for the SKU, compare quantities, and set Mercado Libre only when different. A dry run reads and compares both sides but never writes. Temporary API errors are raised for the worker to retry; missing or duplicate matches create review and never change stock.

- [ ] **Step 5: Run reconciliation tests**

Run: `python -m pytest prod/stock-sync/tests/test_reconcile_handler.py -q`

Expected: PASS.

- [ ] **Step 6: Commit reconciliation**

```bash
git add prod/stock-sync/stock_sync/handlers.py prod/stock-sync/tests/test_reconcile_handler.py
git commit -m "feat: reconcile Mercado Libre from fresh Shopify stock"
```

---

### Task 7: Daily Missed-Order and Full Product Check

**Files:**
- Create: `prod/stock-sync/stock_sync/daily.py`
- Create: `prod/stock-sync/tests/test_daily.py`

**Interfaces:**
- Produces: `run_daily(db, shopify, meli, now, dry_run=False) -> DailyResult`.

- [ ] **Step 1: Write tests that enforce the daily ordering and complete pagination**

```python
def test_daily_enqueues_missed_orders_before_product_checks(ctx):
    ctx.meli.paid_orders = [meli_order("2001")]
    run_daily(ctx.db, ctx.shopify, ctx.meli, ctx.now)
    assert ctx.calls.index("import:2001") < ctx.calls.index("catalog:shopify")

def test_daily_updates_only_meli_when_quantities_differ(ctx):
    ctx.shopify.catalog = [variant("ABC", available=6)]
    ctx.meli.catalog = [listing("ABC", available=9)]
    result = run_daily(ctx.db, ctx.shopify, ctx.meli, ctx.now)
    assert ctx.meli.quantity_writes == [("ABC", 6)]
    assert ctx.shopify.inventory_adjustments == []
    assert result.updated == 1

def test_daily_duplicate_sku_creates_one_review_and_no_write(ctx):
    ctx.shopify.catalog = [variant("ABC", 6), variant("ABC", 5)]
    ctx.meli.catalog = [listing("ABC", 9)]
    run_daily(ctx.db, ctx.shopify, ctx.meli, ctx.now)
    assert ctx.meli.quantity_writes == []
    assert ctx.shopify.review_calls[0].review_key.startswith("product:")
```

- [ ] **Step 2: Run tests and confirm the daily module is missing**

Run: `python -m pytest prod/stock-sync/tests/test_daily.py -q`

Expected: FAIL importing `stock_sync.daily`.

- [ ] **Step 3: Implement missed-order recovery first**

Read checkpoint `daily_orders_completed_at`. On the first run search the previous 30 days; afterwards search from the last successful checkpoint minus one day for overlap. Fully paginate paid orders and run the same idempotent import handler for each before loading either product catalog. Save the checkpoint only after the recovery phase succeeds.

- [ ] **Step 4: Implement the complete product comparison**

Load every managed Shopify SKU and every Mercado Libre item/variation using client pagination. For each SKU present exactly once on both sides, copy Shopify's current quantity when different. Missing, blank, or duplicate SKUs create/update a stable product review draft and do not mutate inventory. Resolve an existing product review when a later daily run finds one safe match on each side.

- [ ] **Step 5: Run daily tests**

Run: `python -m pytest prod/stock-sync/tests/test_daily.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the daily check**

```bash
git add prod/stock-sync/stock_sync/daily.py prod/stock-sync/tests/test_daily.py
git commit -m "feat: add daily full stock reconciliation"
```

---

### Task 8: Worker, Retry Schedule, and Review Commands

**Files:**
- Create: `prod/stock-sync/worker.py`
- Create: `prod/stock-sync/tests/test_worker.py`

**Interfaces:**
- Consumes: all handlers and `run_daily`.
- Produces: `worker.py migrate`, `worker.py once`, `worker.py run`, `worker.py daily`, `worker.py retry <job-id>`, and `worker.py list-review`.

- [ ] **Step 1: Write routing and retry tests**

```python
@pytest.mark.parametrize("attempt,delay", [(1, 60), (2, 300), (3, 1800), (4, 7200)])
def test_retry_schedule(attempt, delay):
    assert retry_delay(attempt) == delay

def test_fifth_failure_moves_job_to_review_after_shopify_notice(worker_ctx):
    worker_ctx.handler.side_effect = RetryableSyncError("still unavailable")
    worker_ctx.job.attempts = 5
    process_one(worker_ctx)
    assert worker_ctx.shopify.review_calls
    assert worker_ctx.db.get_job(worker_ctx.job.id).status == "needs_review"

def test_fifth_failure_stays_retryable_when_shopify_notice_cannot_be_created(worker_ctx):
    worker_ctx.handler.side_effect = RetryableSyncError("still unavailable")
    worker_ctx.shopify.create_or_update_review.side_effect = RetryableSyncError("Shopify unavailable")
    worker_ctx.job.attempts = 5
    process_one(worker_ctx)
    assert worker_ctx.db.get_job(worker_ctx.job.id).status == "retry_wait"

def test_unknown_job_type_is_never_sent_to_a_handler(worker_ctx):
    worker_ctx.job.job_type = "wrong"
    process_one(worker_ctx)
    assert worker_ctx.db.get_job(worker_ctx.job.id).status == "needs_review"
```

- [ ] **Step 2: Run tests and confirm the worker entry point is missing**

Run: `python -m pytest prod/stock-sync/tests/test_worker.py -q`

Expected: FAIL importing `worker`.

- [ ] **Step 3: Implement explicit routing and failure handling**

Route only `import_meli_order`, `shopify_order`, and `reconcile_sku`. On success mark completed. On `RetryableSyncError`, wait 1 minute, 5 minutes, 30 minutes, then 2 hours. On the fifth failure, create/update the Shopify review before marking `needs_review`; if Shopify is temporarily unavailable, keep the job in `retry_wait` so the review notice is not silently lost. On `ReviewRequiredError`, create/update the appropriate Shopify review and then mark `needs_review`. Log every state change without secrets or access tokens.

- [ ] **Step 4: Implement worker commands**

`once` claims at most one job and exits; `run` loops with a short idle pause and handles SIGTERM cleanly; `daily` takes a SQLite lock/checkpoint so two daily runs cannot overlap; `retry` changes one `needs_review` job back to `pending`; `list-review` prints job ID, type, source key, attempts, and last error. `--dry-run` is accepted by `once` and `daily`; after inspection, a queued dry-run job returns to `pending` with its lease cleared and is never marked completed.

- [ ] **Step 5: Run the complete automated suite**

Run: `python -m pytest prod/stock-sync/tests -q && (cd prod/stock-sync && node --test tests/test_receiver.js)`

Expected: all Python and Node tests PASS.

- [ ] **Step 6: Commit the worker**

```bash
git add prod/stock-sync/worker.py prod/stock-sync/tests/test_worker.py
git commit -m "feat: add stock sync worker commands"
```

---

### Task 9: Operations Documentation and Controlled Rollout Verification

**Files:**
- Create: `prod/stock-sync/README.md`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Produces: one documented install, test, run, daily, review, backup, and rollback path.

- [ ] **Step 1: Write the production README**

Document Python and Node versions, `pip install -r requirements.txt`, every environment variable, the Shopify scopes `read_products`, `read_inventory`, `read_orders`, `write_orders`, `write_draft_orders`, and the Mercado Libre seller credentials. Include exact commands for migration, tests, receiver, one worker job, continuous worker, daily dry run, daily apply, listing review jobs, and retrying one job.

- [ ] **Step 2: Add safe local-file ignores**

Ignore only production runtime artifacts: `prod/stock-sync/.env`, `prod/stock-sync/data/*.db*`, `prod/stock-sync/data/meli_tokens.json`, Python caches, and Node test temporary directories. Keep `.env.example`, `schema.sql`, and `data/.gitkeep` tracked.

- [ ] **Step 3: Point the root README to the production service**

Add one short production section linking to `prod/stock-sync/README.md`. Keep unrelated automation descriptions unchanged.

- [ ] **Step 4: Run static and automated verification**

Run: `python -m compileall -q prod/stock-sync && python -m pytest prod/stock-sync/tests -q && (cd prod/stock-sync && node --test tests/test_receiver.js) && git diff --check`

Expected: every command exits 0.

- [ ] **Step 5: Perform the controlled rollout without deleting old processors**

Back up `data/stock_sync.db`, migrate the new database, run one Mercado Libre order in dry-run mode, and manually verify the proposed variant IDs, prices, currency, tags, and inventory behavior. Stop and obtain explicit user confirmation before the first applied import because it changes live Shopify and Mercado Libre data. After confirmation, stop the old processors, apply one reviewed order, confirm exactly one Shopify order and one inventory decrement, then apply one SKU reconciliation and confirm both platforms. Start the continuous worker only after those checks pass.

- [ ] **Step 6: Commit documentation and deployment configuration**

```bash
git add prod/stock-sync/README.md prod/stock-sync/data/.gitkeep .gitignore README.md
git commit -m "docs: add stock sync production operations"
```

## Final Verification

- [ ] Run: `python -m pytest prod/stock-sync/tests -q`
- [ ] Run: `cd prod/stock-sync && node --test tests/test_receiver.js`
- [ ] Run: `python -m compileall -q prod/stock-sync`
- [ ] Run: `git diff --check`
- [ ] Confirm no real Shopify or Mercado Libre API call occurs in the automated tests.
- [ ] Confirm the old processors still exist until the controlled live checks succeed.
- [ ] Review the final branch diff against `docs/superpowers/specs/2026-09-06-simple-stock-sync-design.md` before merge or deployment.

## Current API References

- Shopify imported orders: https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/orderCreate
- Shopify order input: https://shopify.dev/docs/api/admin-graphql/2026-07/input-objects/OrderCreateOrderInput
- Shopify inventory behavior: https://shopify.dev/docs/api/admin-graphql/2026-07/enums/OrderCreateInputsInventoryBehavior
- Shopify review drafts: https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/draftOrderCreate
- Shopify webhook verification: https://shopify.dev/docs/apps/build/webhooks/verify-deliveries
