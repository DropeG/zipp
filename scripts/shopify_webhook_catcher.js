const http = require("http");
const fs = require("fs");
const path = require("path");
const { DatabaseSync } = require("node:sqlite");

const DATA_DIR = path.join(__dirname, "..", "data");
const DB_FILE = path.join(DATA_DIR, "stock_sync.db");
const PORT = Number(process.env.PORT || 3000);

fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new DatabaseSync(DB_FILE);

db.exec(`
  CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    topic TEXT,
    webhook_id TEXT,
    order_id TEXT,
    order_name TEXT,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS stock_tasks (
    task_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    order_id TEXT NOT NULL,
    order_name TEXT,
    line_item_id TEXT,
    sku TEXT,
    shopify_variant_id TEXT,
    quantity_sold INTEGER NOT NULL,
    status TEXT NOT NULL,
    human_note TEXT,
    line_item_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );

  CREATE INDEX IF NOT EXISTS idx_stock_tasks_status
    ON stock_tasks(status);

  CREATE INDEX IF NOT EXISTS idx_stock_tasks_order_id
    ON stock_tasks(order_id);
`);

const insertRawEvent = db.prepare(`
  INSERT INTO raw_events (
    source,
    topic,
    webhook_id,
    order_id,
    order_name,
    received_at,
    payload_json
  ) VALUES (?, ?, ?, ?, ?, ?, ?)
`);

const insertStockTask = db.prepare(`
  INSERT OR IGNORE INTO stock_tasks (
    task_id,
    source,
    order_id,
    order_name,
    line_item_id,
    sku,
    shopify_variant_id,
    quantity_sold,
    status,
    human_note,
    line_item_json,
    created_at,
    updated_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`);

function buildTaskId(orderId, item) {
  if (item.id) {
    return `shopify:${orderId}:${item.id}`;
  }

  return `shopify:${orderId}:${item.variant_id || "no-variant"}:${item.sku || "no-sku"}`;
}

function classifyLineItem(item) {
  const sku = (item.sku || "").trim();

  if (!sku) {
    return {
      status: "needs_review",
      humanNote: "Line item no tiene SKU. No se puede sincronizar por identidad compartida Shopify/Meli.",
    };
  }

  if (!item.variant_id) {
    return {
      status: "skipped_no_shopify_variant",
      humanNote: "Line item no tiene Shopify variant_id. No se sincronizo porque no se puede confirmar inventario/variante con seguridad.",
    };
  }

  return {
    status: "pending",
    humanNote: null,
  };
}

function sendText(res, statusCode, text) {
  res.writeHead(statusCode, { "Content-Type": "text/plain" });
  res.end(text);
}

function readRequestBody(req, onBody) {
  let body = "";

  req.on("data", chunk => {
    body += chunk;
  });

  req.on("end", () => onBody(body));
}

function parseJsonBody(body, res) {
  try {
    return JSON.parse(body);
  } catch (error) {
    console.log("Webhook invalido, no es JSON:", error.message);
    sendText(res, 400, "Invalid JSON");
    return null;
  }
}

function handleShopifyOrderCreate(req, res) {
  readRequestBody(req, body => {
    const webhookId = req.headers["x-shopify-webhook-id"];
    const topic = req.headers["x-shopify-topic"];
    const shop = req.headers["x-shopify-shop-domain"];

    const payload = parseJsonBody(body, res);
    if (!payload) return;

    const shopifyOrderId = String(payload.id);
    const orderName = payload.name;
    const receivedAt = new Date().toISOString();

    insertRawEvent.run(
      "shopify",
      topic || null,
      webhookId || null,
      shopifyOrderId,
      orderName || null,
      receivedAt,
      JSON.stringify(payload)
    );

    console.log("Raw event Shopify guardado");
    console.log("Webhook ID:", webhookId || "(sin webhook id)");
    console.log("Topic:", topic || "(sin topic)");
    console.log("Shop:", shop || "(sin shop)");
    console.log("Order ID:", shopifyOrderId);
    console.log("Order name:", orderName);

    for (const item of payload.line_items || []) {
      const taskId = buildTaskId(shopifyOrderId, item);
      const { status, humanNote } = classifyLineItem(item);
      const sku = (item.sku || "").trim() || null;
      const lineItemId = item.id ? String(item.id) : null;
      const variantId = item.variant_id ? String(item.variant_id) : null;
      const quantity = Number(item.quantity || 0);
      const result = insertStockTask.run(
        taskId,
        "shopify",
        shopifyOrderId,
        orderName || null,
        lineItemId,
        sku,
        variantId,
        quantity,
        status,
        humanNote,
        JSON.stringify(item),
        receivedAt,
        receivedAt
      );

      if (result.changes === 0) {
        console.log(`Stock task ya existia, no se duplico: ${sku || "(sin SKU)"}`);
      } else {
        console.log(`Stock task creada: ${sku || "(sin SKU)"} -> ${status}`);
      }

      if (humanNote) {
        console.log(`  Nota: ${humanNote}`);
      }
    }

    sendText(res, 200, "OK");
  });
}

function handleMeliOrderNotification(req, res) {
  readRequestBody(req, body => {
    const payload = parseJsonBody(body, res);
    if (!payload) return;

    const topic = payload.topic || payload.type || null;
    const resource = payload.resource || payload._resource || null;
    const userId = payload.user_id || payload.userId || null;
    const notificationId = payload._id || payload.id || null;
    const receivedAt = new Date().toISOString();
    const orderId = resource ? String(resource).split("/").filter(Boolean).pop() : null;

    insertRawEvent.run(
      "meli",
      topic,
      notificationId ? String(notificationId) : null,
      orderId,
      null,
      receivedAt,
      JSON.stringify(payload)
    );

    console.log("Raw event Meli guardado");
    console.log("Topic:", topic || "(sin topic)");
    console.log("Resource:", resource || "(sin resource)");
    console.log("Order ID:", orderId || "(sin order id)");
    console.log("User ID:", userId || "(sin user id)");

    sendText(res, 200, "OK");
  });
}

const server = http.createServer((req, res) => {
  if (req.method === "POST" && req.url === "/webhooks/shopify/orders-create") {
    handleShopifyOrderCreate(req, res);
    return;
  }

  if (req.method === "POST" && req.url === "/webhooks/meli/orders") {
    handleMeliOrderNotification(req, res);
    return;
  }

  sendText(res, 404, "Not found");
});

server.listen(PORT, () => {
  console.log(`Escuchando en http://localhost:${PORT}`);
  console.log("Guardando datos en:", DB_FILE);
});
