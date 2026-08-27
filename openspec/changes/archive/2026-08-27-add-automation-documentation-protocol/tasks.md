## 1. Repository Documentation Protocol

- [x] 1.1 Create `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md` with the standard documentation structure for future automations.
- [x] 1.2 Define the purpose of `README.md`, `HANDOFF.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `LOCAL_TESTING.md`, `TROUBLESHOOTING.md`, `PENDING.md`, and optional historical/context documents.
- [x] 1.3 Include guidance that automation-specific documentation belongs under `automations/<automation-id>/docs/`.
- [x] 1.4 Include guidance for AI agents documenting future automations without presenting incomplete parking folders as active automations.

## 2. Stock Sync Local Testing Documentation

- [x] 2.1 Create `automations/stock-sync/docs/LOCAL_TESTING.md`.
- [x] 2.2 Document local prerequisites: `.env`, `meli_tokens.json`, Python virtualenv, Node.js, `cloudflared`, and access to Shopify/Mercado Libre.
- [x] 2.3 Document how to start the local webhook catcher on `localhost:3000`.
- [x] 2.4 Document how to expose the local catcher with a temporary `cloudflared` tunnel.
- [x] 2.5 Document temporary Shopify and Mercado Libre webhook URLs for the tunnel.
- [x] 2.6 Document how to generate or wait for a test event and inspect `raw_events` and `stock_tasks` in SQLite.
- [x] 2.7 Document dry-run commands for both Shopify -> Mercado Libre and Mercado Libre -> Shopify.
- [x] 2.8 Document controlled apply commands with `--limit 1` and clear human review before touching real stock.
- [x] 2.9 Document local cleanup after testing, including stopping the tunnel and removing temporary webhook configuration.

## 3. Navigation And Validation

- [x] 3.1 Update `automations/stock-sync/README.md` to link to `docs/LOCAL_TESTING.md`.
- [x] 3.2 Update the root `README.md` only as needed to point future maintainers or AI agents to the repository documentation protocol.
- [x] 3.3 Validate the OpenSpec change.
- [x] 3.4 Review documentation wording to confirm local testing is framed as human testing, not troubleshooting.
