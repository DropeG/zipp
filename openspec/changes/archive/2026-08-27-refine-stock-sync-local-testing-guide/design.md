## Context

The stock-sync automation now has `docs/LOCAL_TESTING.md`, but the first real local test showed where the guide can be clearer for a human reader. The tester saw `localhost:3000` in the browser, created a paid Shopify test order, confirmed the webhook arrived through `cloudflared`, ran dry-run, and then needed help understanding how to avoid applying the test task and restore the Shopify test order/stock.

The documentation should capture this lived path directly, so the next person can follow it without already understanding webhooks, tunnels, or SQLite state.

## Goals / Non-Goals

**Goals:**

- Make `LOCAL_TESTING.md` feel like a guided human walkthrough.
- Explain in plain language that `localhost:3000` is not a visual website; it is a webhook receiver.
- Include a concrete Shopify -> Mercado Libre test flow with realistic evidence from logs and SQLite.
- Explain how to stop before applying stock when the goal is only to prove webhook delivery.
- Explain how to clean up a test order and neutralize a local `ready_to_apply` task after a test-only run.

**Non-Goals:**

- Change synchronizer code.
- Add automation for Shopify refunds, order cancellation, or SQLite cleanup.
- Replace the existing operations or troubleshooting documents.
- Require a real `--apply` during local testing.

## Decisions

### Put The Guided Walkthrough Near The Top

The guide should first explain the mental model and then offer a "Prueba guiada Shopify -> Meli" section. This matches the path a human naturally follows during the first test.

Alternative considered: keep only generic commands. That is technically accurate, but it leaves too much interpretation to the reader.

### Treat `--apply` As Optional

The guide should clearly say that proving webhook delivery and dry-run is enough for many tests. `--apply` remains available, but not required when the goal is only confirmation.

Alternative considered: always continue to `--apply`. That is riskier because local testing can use real products and real stock.

### Document Cleanup As Part Of The Normal Test Flow

Cleanup belongs in local testing because the human test creates real Shopify state. The guide should mention cancel/refund, restock, remove temporary webhooks, close tunnel/catcher, and mark the local task so it will not be applied accidentally.

Alternative considered: leave cleanup to troubleshooting. The user clarified this is not troubleshooting; it is part of how a human finishes a test safely.

## Risks / Trade-offs

- The guide may include examples that look too tied to one order number -> Use realistic examples but mark values as examples, not constants.
- Manual SQLite update can be risky -> Present it only as a test-only cleanup step for the specific order created during the test.
- Human skips cleanup -> Add an explicit checklist for success and cleanup.
- Reader confuses dry-run with apply -> Repeat that dry-run does not touch Mercado Libre, while apply does.
