const { createHmac, randomUUID, timingSafeEqual } = require("node:crypto");
const { mkdirSync, readFileSync } = require("node:fs");
const { createServer } = require("node:http");
const { dirname, join } = require("node:path");
const { DatabaseSync } = require("node:sqlite");

const schemaPath = join(__dirname, "schema.sql");
const schema = readFileSync(schemaPath, "utf8");

class HttpError extends Error {
  constructor(status) {
    super(String(status));
    this.status = status;
  }
}

function configuredShopDomain(shopUrl) {
  const hostname = new URL(shopUrl).hostname.toLowerCase();
  if (!hostname.endsWith(".myshopify.com")) {
    throw new Error("SHOPIFY_SHOP_URL must use a myshopify.com domain");
  }
  return hostname;
}

function validSecret(expected, received) {
  if (typeof expected !== "string" || typeof received !== "string") return false;
  const expectedBuffer = Buffer.from(expected);
  const receivedBuffer = Buffer.from(received);
  return expectedBuffer.length === receivedBuffer.length
    && timingSafeEqual(expectedBuffer, receivedBuffer);
}

function validShopifyHmac(rawBody, secret, header) {
  if (typeof header !== "string") return false;
  const expected = createHmac("sha256", secret).update(rawBody).digest();
  const received = Buffer.from(header, "base64");
  return received.length === expected.length && timingSafeEqual(received, expected);
}

async function readRawBody(request, maxBytes) {
  const contentLength = Number(request.headers["content-length"]);
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    request.resume();
    throw new HttpError(413);
  }

  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new HttpError(413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function parseJsonObject(rawBody) {
  try {
    const parsed = JSON.parse(rawBody.toString("utf8"));
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not object");
    return parsed;
  } catch {
    throw new HttpError(400);
  }
}

function orderId(value) {
  if ((typeof value !== "string" && typeof value !== "number") || String(value).trim() === "") {
    throw new HttpError(400);
  }
  return String(value);
}

function prepareDatabase(databasePath) {
  mkdirSync(dirname(databasePath), { recursive: true });
  const database = new DatabaseSync(databasePath);
  database.exec("PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL;");
  database.exec(schema);
  database.close();
}

function saveEventAndJob(databasePath, event, job) {
  const database = new DatabaseSync(databasePath);
  try {
    database.exec("PRAGMA synchronous = FULL; BEGIN IMMEDIATE;");
    const eventResult = database.prepare(
      "INSERT INTO events (source, event_key, payload, received_at) VALUES (?, ?, ?, ?) ON CONFLICT(source, event_key) DO NOTHING"
    ).run(event.source, event.eventKey, event.payload, new Date().toISOString());
    if (eventResult.changes > 0) {
      const now = new Date().toISOString();
      database.prepare(
        "INSERT INTO jobs (job_type, source_key, resource_key, payload, status, attempts, available_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?) ON CONFLICT(job_type, source_key) DO NOTHING"
      ).run(job.jobType, job.sourceKey, job.resourceKey, job.payload, now, now, now);
    }
    database.exec("COMMIT;");
  } catch (error) {
    try {
      database.exec("ROLLBACK;");
    } catch {
      // No transaction was open, so there is nothing to roll back.
    }
    throw error;
  } finally {
    database.close();
  }
}

function createReceiver(options) {
  const config = {
    databasePath: options.databasePath,
    shopifyWebhookSecret: options.shopifyWebhookSecret,
    shopifyDomain: configuredShopDomain(options.shopifyShopUrl),
    meliWebhookToken: options.meliWebhookToken,
    maxWebhookBytes: options.maxWebhookBytes,
  };
  if (!config.databasePath || !config.shopifyWebhookSecret || !config.meliWebhookToken
    || !Number.isSafeInteger(config.maxWebhookBytes) || config.maxWebhookBytes <= 0) {
    throw new Error("Receiver configuration is incomplete");
  }
  prepareDatabase(config.databasePath);

  return createServer(async (request, response) => {
    try {
      if (request.method === "GET" && request.url === "/health") {
        response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
        response.end("ok");
        return;
      }

      if (request.method === "POST" && request.url === "/webhooks/shopify/orders-create") {
        const rawBody = await readRawBody(request, config.maxWebhookBytes);
        if (!validShopifyHmac(rawBody, config.shopifyWebhookSecret, request.headers["x-shopify-hmac-sha256"])) {
          throw new HttpError(401);
        }
        if (request.headers["x-shopify-topic"] !== "orders/create"
          || String(request.headers["x-shopify-shop-domain"] || "").toLowerCase() !== config.shopifyDomain
          || !request.headers["x-shopify-webhook-id"]) {
          throw new HttpError(400);
        }
        const order = parseJsonObject(rawBody);
        const id = orderId(order.id);
        saveEventAndJob(config.databasePath, {
          source: "shopify",
          eventKey: request.headers["x-shopify-webhook-id"],
          payload: rawBody.toString("utf8"),
        }, {
          jobType: "shopify_order",
          sourceKey: `shopify-order:${id}`,
          resourceKey: `order:${id}`,
          payload: rawBody.toString("utf8"),
        });
        response.writeHead(200);
        response.end();
        return;
      }

      if (request.method === "POST" && request.url === "/webhooks/meli") {
        const rawBody = await readRawBody(request, config.maxWebhookBytes);
        if (!validSecret(config.meliWebhookToken, request.headers["x-webhook-token"])) throw new HttpError(401);
        const notice = parseJsonObject(rawBody);
        const match = typeof notice.resource === "string" && /^\/orders\/(\d+)$/.exec(notice.resource);
        if (!match) throw new HttpError(400);
        const id = match[1];
        // Delivery identity is separate from order-import identity. Without a
        // provider notification ID, accepting a redundant recheck is safer
        // than permanently dropping a later paid notification for this order.
        const deliveryId = typeof notice._id === "string" && notice._id.trim()
          ? notice._id : randomUUID();
        const eventKey = `meli-notice:${id}:${deliveryId}`;
        saveEventAndJob(config.databasePath, {
          source: "meli",
          eventKey,
          payload: rawBody.toString("utf8"),
        }, {
          jobType: "import_meli_order",
          sourceKey: eventKey,
          resourceKey: `order:${id}`,
          payload: JSON.stringify({ order_id: id }),
        });
        response.writeHead(200);
        response.end();
        return;
      }

      response.writeHead(404);
      response.end();
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 503;
      response.writeHead(status);
      response.end();
    }
  });
}

function receiverFromEnvironment() {
  return createReceiver({
    databasePath: process.env.STOCK_SYNC_DATABASE || join(__dirname, "data", "stock_sync.db"),
    shopifyWebhookSecret: process.env.SHOPIFY_WEBHOOK_SECRET,
    shopifyShopUrl: process.env.SHOPIFY_SHOP_URL,
    meliWebhookToken: process.env.MELI_WEBHOOK_TOKEN,
    maxWebhookBytes: Number(process.env.MAX_WEBHOOK_BYTES || "1048576"),
  });
}

function receiverListenOptionsFromEnvironment(environment = process.env) {
  return {
    host: environment.HOST || "127.0.0.1",
    port: Number(environment.PORT || "3000"),
  };
}

function startReceiverFromEnvironment(environment = process.env, create = receiverFromEnvironment) {
  const server = create();
  const { host, port } = receiverListenOptionsFromEnvironment(environment);
  server.listen(port, host, () => {
    process.stdout.write("stock-sync receiver listening\n");
  });
  return server;
}

if (require.main === module) {
  startReceiverFromEnvironment();
}

module.exports = {
  createReceiver,
  receiverFromEnvironment,
  receiverListenOptionsFromEnvironment,
  startReceiverFromEnvironment,
};
