---
name: shopify-to-meli-one-by-one
description: Discover existing exact-SKU Shopify-to-Mercado-Libre mappings, then publish the next unsynced active Shopify product to Mercado Libre Chile, exactly one product per invocation. Use for initial live reconciliation, the cron-dispatched catalog migration, a manual one-product run, or a non-publishing preview. Do not use for ongoing stock synchronization or bulk publication.
---

# Shopify to Mercado Libre, one by one

Publish at most one Shopify product per invocation. The scheduled invocation is
production-authorized; a request that explicitly says preview, inspect, test,
or dry-run is not.

Run from this skill directory with `.venv/bin/python scripts/meli.py`.
Operator-only installation and initial reconciliation are in
[setup.md](references/setup.md); a scheduled product chat does not need to read
that reference because `next` refuses unless setup is complete.

## First-time live reconciliation

Before enabling production, follow the bootstrap procedure in
[setup.md](references/setup.md). It scans all Shopify products and all seller
items in Mercado Libre, joins only one-to-one exact SKU matches, and writes the
verified mappings locally. It never creates or updates a Mercado Libre listing.
A production `next` refuses to run until bootstrap has been applied and skips
products placed on reconciliation hold because of ambiguous or conflicting
SKU evidence.

## Production run

The cron dispatcher may already have selected the product before opening this
Paseo chat. Still run `next`: it safely returns the same pending product instead
of selecting another one.

1. Get the next product:

   ```bash
   .venv/bin/python scripts/meli.py next
   ```

   Read `work/source.json`. If the result is `no-op`, report that all eligible
   products are synced and stop. A pending product is intentionally returned
   again after an interrupted run. The command refreshes Shopify's complete SKU
   ownership snapshot and skips products affected by SKU reuse or mapped-SKU
   drift.

2. Check `source.resume.reuse_existing_payload`. When it is `true`, a Mercado
   Libre item already exists and the payload hash is locked. Confirm that
   `work/payload.json` exists and has the same `shopify_id`, then run `publish`
   once without editing the payload, re-reading the listing guide, downloading
   images, or reconsidering category/content. Read `work/result.json`, report
   the outcome, and stop. If the payload is missing or belongs to another
   product, report that manual review is required; never rebuild a payload for
   an already-created item.

3. Otherwise, read [listing-guide.md](references/listing-guide.md), inspect the Shopify
   product and images, and create `work/payload.json`. The agent chooses the
   category, buyer-facing content, attributes, and image order. The script owns
   SKU, price, stock, seller identity, publication model, and API state.
   If `work/result.json` contains a prior `validation_failed` result for this
   same Shopify product, use its causes as feedback instead of blindly reusing
   the prior payload.

   If the initial category is wrong, use the read-only helper as needed:

   ```bash
   .venv/bin/python scripts/meli.py category --query "functional product name"
   .venv/bin/python scripts/meli.py category --id MLC123
   ```

4. Publish:

   ```bash
   .venv/bin/python scripts/meli.py publish
   ```

   This command first runs local checks and Mercado Libre `/items/validate`.
   It creates nothing when validation fails. If validation succeeds, it
   creates or resumes the item or User Products family, adds the description,
   verifies the live SKU/price/stock/category, and saves the mapping.

5. Read `work/result.json`. In a scheduled Paseo chat, the environment contains
   `PRODUCT_SYNC_PASEO_WORKSPACE_ID`; before the final response, run:

   ```bash
   /usr/bin/python3 scripts/mark_workspace.py
   ```

   This leaves successful workspace names unchanged, prefixes retryable runs
   with `[R]`, and prefixes blocked, validation-failed, or review-required runs
   with `[F]`. Then report the outcome. Stop after this product; never select a
   second product in the same invocation.

If truthful required information is unavailable or current policy prohibits
the product, persist a concise terminal reason and stop:

```bash
.venv/bin/python scripts/meli.py block --reason "Concrete terminal reason"
```

Do not block authentication, network, rate-limit, or server failures. Those
remain pending and the next scheduled invocation retries the same product.

## Dry-run

For an explicitly non-production request:

```bash
.venv/bin/python scripts/meli.py next --dry-run
# Create work/payload.json
.venv/bin/python scripts/meli.py publish --dry-run
```

Dry-run performs authenticated reads and Mercado Libre validation but creates
no listing and changes no mapping, pending, or blocker state. Do not run
`block` during dry-run.

## Non-negotiable boundaries

- Use exactly the Shopify SKU, price, inventory, variants, and source images;
  never invent or inflate them.
- Follow the `publication_model` in `source.json`. The script derives it from
  Mercado Libre's current `user_product_seller` capability and preserves an
  existing family model.
- Never create when an exact SKU is ambiguous or an existing exact-SKU listing
  is not fully consistent with the proposed item.
- Never edit or rebuild `work/payload.json` when
  `source.resume.reuse_existing_payload` is `true`; the existing item makes its
  payload hash immutable.
- Never bypass bootstrap holds or current Shopify SKU-ownership holds. The
  publish command checks current ownership again immediately before validation.
- Remove Zipp/store/contact details from Mercado Libre buyer-facing content.
- Do not modify Shopify, the stock-sync service, this skill, or its reference
  files during a product run.
- Ongoing inventory changes belong to `prod/stock-sync`, which reconciles the
  published listings by exact SKU.
