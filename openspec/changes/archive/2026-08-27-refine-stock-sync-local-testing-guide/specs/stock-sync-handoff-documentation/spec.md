## ADDED Requirements

### Requirement: Stock sync local testing is human-guided
The stock-sync local testing documentation SHALL guide a human through the validated Shopify -> Mercado Libre local test path using plain-language explanations, expected evidence, and cleanup steps.

#### Scenario: Human understands the tunnel
- **WHEN** a human reads the local testing guide before configuring Shopify
- **THEN** the guide explains that `localhost:3000` is a webhook receiver rather than a visual web page and that `cloudflared` provides a temporary public URL pointing to it

#### Scenario: Human validates Shopify webhook delivery
- **WHEN** a human creates a paid Shopify test order with a real SKU
- **THEN** the guide explains how to recognize successful catcher output, SQLite `raw_events` insertion, and a new `stock_tasks` row

#### Scenario: Human validates dry-run without touching Mercado Libre
- **WHEN** a human runs the Shopify -> Mercado Libre dry-run
- **THEN** the guide explains how to read the output that shows Shopify stock, the matched Mercado Libre item, current Mercado Libre stock, and the `ready_to_apply` target

#### Scenario: Human stops after webhook/dry-run proof
- **WHEN** the goal of the local test is only to confirm webhook delivery and dry-run behavior
- **THEN** the guide explains that `--apply` is not required and describes cleanup for the Shopify test order, temporary webhook, tunnel, catcher, and local pending/apply-ready task
