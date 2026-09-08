const assert = require("node:assert/strict");
const { createHmac } = require("node:crypto");
const { mkdtemp, readFile, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const test = require("node:test");
const { DatabaseSync } = require("node:sqlite");

const {
  createReceiver,
  receiverListenOptionsFromEnvironment,
  startReceiverFromEnvironment,
} = require("../receiver");

const shopifySecret = "shopify-secret";
const orderPayload = JSON.stringify({ id: "1001", line_items: [{ sku: "ABC" }] });

test("production startup passes loopback default host to listen", () => {
  let listenArguments;
  const server = {
    listen(...arguments_) {
      listenArguments = arguments_;
    },
  };

  assert.equal(startReceiverFromEnvironment({}, () => server), server);
  assert.deepEqual(listenArguments.slice(0, 2), [3000, "127.0.0.1"]);
});

test("receiver listener honors configured host", () => {
  assert.deepEqual(receiverListenOptionsFromEnvironment({ HOST: "127.0.0.2", PORT: "3010" }), {
    host: "127.0.0.2",
    port: 3010,
  });
});

function countRows(dbPath, table) {
  const database = new DatabaseSync(dbPath);
  const count = database.prepare(`SELECT COUNT(*) AS count FROM ${table}`).get().count;
  database.close();
  return count;
}

function jobFor(dbPath, jobType) {
  const database = new DatabaseSync(dbPath);
  const job = database.prepare(
    "SELECT job_type, source_key, resource_key, payload FROM jobs WHERE job_type = ?"
  ).get(jobType);
  database.close();
  return { ...job };
}

function signedShopifyHeaders(body, deliveryId = "delivery-1", topic = "orders/create") {
  return {
    "x-shopify-hmac-sha256": createHmac("sha256", shopifySecret).update(body).digest("base64"),
    "x-shopify-topic": topic,
    "x-shopify-shop-domain": "example.myshopify.com",
    "x-shopify-webhook-id": deliveryId,
  };
}

async function startReceiver() {
  const directory = await mkdtemp(join(tmpdir(), "stock-sync-receiver-"));
  const dbPath = join(directory, "stock-sync.sqlite");
  const server = createReceiver({
    databasePath: dbPath,
    shopifyWebhookSecret: shopifySecret,
    shopifyShopUrl: "https://example.myshopify.com",
    meliWebhookToken: "secret",
    maxWebhookBytes: 1024,
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  return {
    dbPath,
    async close() {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
      await rm(directory, { recursive: true, force: true });
    },
    get(path) {
      return fetch(`http://127.0.0.1:${port}${path}`);
    },
    postRaw(path, body, headers) {
      return fetch(`http://127.0.0.1:${port}${path}`, { method: "POST", headers, body });
    },
    postShopify(body, headers) {
      return this.postRaw("/webhooks/shopify/orders-create", body, headers);
    },
    postSignedShopify(body, deliveryId) {
      return this.postShopify(body, signedShopifyHeaders(body, deliveryId));
    },
    postShopifyCancelled(body, deliveryId = "cancel-delivery-1") {
      return this.postRaw(
        "/webhooks/shopify/orders-cancelled",
        body,
        signedShopifyHeaders(body, deliveryId, "orders/cancelled"),
      );
    },
  };
}

test("rejects an invalid Shopify HMAC without saving an event", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  const response = await receiver.postShopify("{}", { "x-shopify-hmac-sha256": "bad" });

  assert.equal(response.status, 401);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
});

test("deduplicates Shopify delivery IDs", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  assert.equal((await receiver.postSignedShopify(orderPayload, "delivery-1")).status, 200);
  assert.equal((await receiver.postSignedShopify(orderPayload, "delivery-1")).status, 200);

  assert.equal(countRows(receiver.dbPath, "events"), 1);
  assert.equal(countRows(receiver.dbPath, "jobs"), 1);
  assert.deepEqual(jobFor(receiver.dbPath, "shopify_order"), {
    job_type: "shopify_order",
    source_key: "shopify-order:1001",
    resource_key: "order:1001",
    payload: orderPayload,
  });
});

test("accepts and deduplicates Shopify order cancellation deliveries", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  assert.equal((await receiver.postShopifyCancelled(orderPayload)).status, 200);
  assert.equal((await receiver.postShopifyCancelled(orderPayload)).status, 200);

  assert.equal(countRows(receiver.dbPath, "events"), 1);
  assert.equal(countRows(receiver.dbPath, "jobs"), 1);
  assert.deepEqual(jobFor(receiver.dbPath, "shopify_cancelled"), {
    job_type: "shopify_cancelled",
    source_key: "shopify-cancelled:1001",
    resource_key: "order:1001",
    payload: orderPayload,
  });
});

test("rejects oversized bodies before JSON parsing", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  const response = await receiver.postRaw("/webhooks/meli", "x".repeat(1025), {
    "x-webhook-token": "secret",
  });

  assert.equal(response.status, 413);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
});

test("rejects Shopify requests for a different topic, shop, or missing delivery ID", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  const wrongTopic = await receiver.postShopify(orderPayload, {
    ...signedShopifyHeaders(orderPayload),
    "x-shopify-topic": "orders/updated",
  });
  const wrongShop = await receiver.postShopify(orderPayload, {
    ...signedShopifyHeaders(orderPayload),
    "x-shopify-shop-domain": "other.myshopify.com",
  });
  const noDeliveryId = await receiver.postShopify(orderPayload, {
    ...signedShopifyHeaders(orderPayload),
    "x-shopify-webhook-id": "",
  });

  assert.equal(wrongTopic.status, 400);
  assert.equal(wrongShop.status, 400);
  assert.equal(noDeliveryId.status, 400);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
});

test("rejects an authenticated Shopify body without an order ID", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());
  const body = JSON.stringify({ line_items: [] });

  const response = await receiver.postSignedShopify(body, "delivery-without-order");

  assert.equal(response.status, 400);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
});

test("accepts a Mercado Libre order notice and queues the verified import", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());
  const body = JSON.stringify({ resource: "/orders/2001", topic: "orders_v2", _id: "notice-1" });

  const response = await receiver.postRaw("/webhooks/meli", body, { "x-webhook-token": "secret" });

  assert.equal(response.status, 200);
  assert.equal(countRows(receiver.dbPath, "events"), 1);
  assert.deepEqual(jobFor(receiver.dbPath, "import_meli_order"), {
    job_type: "import_meli_order",
    source_key: "meli-notice:2001:notice-1",
    resource_key: "order:2001",
    payload: JSON.stringify({ order_id: "2001" }),
  });
});

test("controlled rollout guide uses the deterministic Mercado Libre delivery key", async () => {
  const guide = await readFile(join(__dirname, "..", "README.md"), "utf8");

  assert.ok(guide.includes('_id: `first-rollout-${process.env.KNOWN_ORDER_ID}`'));
  assert.match(guide, /f"meli-notice:\{order_id\}:first-rollout-\{order_id\}"/);
  assert.match(guide, /f"order:\{order_id\}"/);
});

test("rejects Mercado Libre notices with an invalid token or resource", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  const badToken = await receiver.postRaw("/webhooks/meli", JSON.stringify({ resource: "/orders/2001" }), {
    "x-webhook-token": "wrong",
  });
  const badResource = await receiver.postRaw("/webhooks/meli", JSON.stringify({ resource: "/questions/2001" }), {
    "x-webhook-token": "secret",
  });

  assert.equal(badToken.status, 401);
  assert.equal(badResource.status, 400);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
});

test("later Mercado Libre notification rechecks an order after an unpaid delivery completed", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());
  const headers = { "x-webhook-token": "secret" };
  const notice = { resource: "/orders/2001", topic: "orders_v2", _id: "notice-unpaid" };
  assert.equal((await receiver.postRaw("/webhooks/meli", JSON.stringify(notice), headers)).status, 200);
  const database = new DatabaseSync(receiver.dbPath);
  database.exec("UPDATE jobs SET status = 'completed';");
  database.close();
  notice._id = "notice-paid";
  assert.equal((await receiver.postRaw("/webhooks/meli", JSON.stringify(notice), headers)).status, 200);
  assert.equal((await receiver.postRaw("/webhooks/meli", JSON.stringify(notice), headers)).status, 200);
  const read = new DatabaseSync(receiver.dbPath);
  assert.equal(read.prepare("SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'").get().n, 1);
  assert.equal(read.prepare("SELECT COUNT(*) AS n FROM jobs WHERE resource_key = 'order:2001'").get().n, 2);
  assert.equal(countRows(receiver.dbPath, "events"), 2);
  read.close();
});

test("Mercado Libre notices without delivery identity cannot permanently suppress future rechecks", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());
  const body = JSON.stringify({ resource: "/orders/2001", topic: "orders_v2" });
  for (let n = 0; n < 2; n++) {
    assert.equal((await receiver.postRaw("/webhooks/meli", body, { "x-webhook-token": "secret" })).status, 200);
  }
  assert.equal(countRows(receiver.dbPath, "jobs"), 2);
});

test("returns 503 and rolls back the event when SQLite cannot save its job", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());
  const database = new DatabaseSync(receiver.dbPath);
  database.exec("CREATE TRIGGER reject_jobs BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'job insert failed'); END;");
  database.close();

  const response = await receiver.postSignedShopify(orderPayload, "delivery-with-rejected-job");

  assert.equal(response.status, 503);
  assert.equal(countRows(receiver.dbPath, "events"), 0);
  assert.equal(countRows(receiver.dbPath, "jobs"), 0);
});

test("reports health without requiring webhook authentication", async (t) => {
  const receiver = await startReceiver();
  t.after(() => receiver.close());

  const response = await receiver.get("/health");

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "ok");
});
