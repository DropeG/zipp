# Product Publishing: Shopify -> Mercado Libre

Publishes at most one eligible Shopify product to Mercado Libre Chile per run.
The guarded workflow uses agent reasoning for buyer-facing copy and supported
attributes, while deterministic Python gates enforce duplicate control,
inventory fidelity, original images and live-publication verification.

## Safe entry points

From the repository root:

~~~bash
PY=./venv/bin/python

# Select one product and fetch only its Mercado Libre requirements.
$PY automations/product-publishing/scripts/one_by_one_sync.py prepare \
  --mode dry-run

# If the initial category is semantically wrong, add source-supported
# candidates and select one with an auditable reason (maximum five).
$PY automations/product-publishing/scripts/one_by_one_sync.py discover-category \
  --query "FUNCTIONAL PRODUCT QUERY" --reason "Shopify evidence"
$PY automations/product-publishing/scripts/one_by_one_sync.py select-category \
  --category-id MLC123 --reason "Type/function/attribute fit"

# After preparing data/product-publishing/one-by-one/productos_listos.json:
$PY automations/product-publishing/scripts/one_by_one_sync.py check-payload

# Mercado Libre validation only; creates no listing.
$PY automations/product-publishing/scripts/one_by_one_sync.py submit
~~~

Live publication requires explicit authorization:

~~~bash
$PY automations/product-publishing/scripts/one_by_one_sync.py prepare \
  --mode publish
$PY automations/product-publishing/scripts/one_by_one_sync.py check-payload
$PY automations/product-publishing/scripts/one_by_one_sync.py submit --publish
~~~

The full agent procedure is in
'.agents/skills/shopify-to-meli-one-by-one-sync/SKILL.md'.

## State

- 'sync_mappings.json': completed Shopify-to-Meli mappings. Each Shopify
  product maps to one Mercado Libre item, including its full variant matrix.
- 'sync_blocked_products.json': terminal product blockers; local runtime state.
- 'publication_journal.json': created items awaiting completion; local runtime
  state used to prevent replacement duplicates.
- 'selection_reservations.json': selected products awaiting validation or
  publication; prevents overlapping cron runs from moving to another product.
- 'publication_quality_records.json': durable post-live quality results,
  including achieved and omitted objectives with reasons.
- 'data/product-publishing/one-by-one/': generated source, payload, audit and
  result files.

State writes are atomic. Dry-run does not modify mapping, blocker or journal
state.

Before creation, the workflow searches the seller's own items through both
official SKU filters. A unique faithful match may be adopted and mapped;
ambiguous or structurally inconsistent matches are blocked for human review so
the workflow never creates a duplicate opportunistically.

## Variants

For a multi-variant Shopify product, the workflow sends one Mercado Libre item
with every Shopify variant, preserving each variant's own SKU, price and stock.
Variants at stock zero remain in that item with `available_quantity: 0`; they
are not omitted or split into separate Meli listings. A later stock update is
the responsibility of the stock-sync automation.

## Outcomes

- 'no-op': nothing eligible.
- 'validated': exactly one payload passed dry-run validation.
- 'published': exactly one item passed repeated live status checks, attribute
  persistence and mapping.
- 'blocked': terminal product, data, image, policy or moderation problem.
- 'failed_retryable': network, auth or service failure; resume the same
  journaled item.

## Legacy scripts

'sync_products.py' is the original bulk/Gemini experiment. It is retained only
for historical compatibility and is not the supported production entry point.
'fetch_meli_requirements.py' is a compatibility wrapper around the guarded
prepare command.

Historical notes are in 'docs/HISTORICAL_PLAN.md'.

## Read-only compatibility replay

Use the compatibility harness to compare reconstructed payloads for exactly
three or five already-mapped products with their current Shopify source and
existing Mercado Libre listings. Collection uses only GET requests, refuses to
refresh an expired Meli token, and writes only to the chosen output directory.
It never validates, publishes or edits a listing.

~~~bash
$PY automations/product-publishing/scripts/compatibility_replay.py collect \
  --shopify-id SHOPIFY_ID_1 \
  --shopify-id SHOPIFY_ID_2 \
  --shopify-id SHOPIFY_ID_3 \
  --output-dir /isolated/replay

# After both skills reconstruct their payloads inside each case directory,
# optionally call only Meli's non-publishing validation endpoint:
$PY automations/product-publishing/scripts/compatibility_replay.py validate \
  --configuration both \
  --output-dir /isolated/replay

# Aggregate the source, historical-listing and validation evidence:
$PY automations/product-publishing/scripts/compatibility_replay.py compare \
  --output-dir /isolated/replay
~~~

Treat price and inventory differences in the historical listing as temporal
drift unless a reconstructed payload also disagrees with the current Shopify
fixture. Review category drift manually because Mercado Libre's prediction
results and category tree can change over time.
