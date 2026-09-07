---
name: shopify-to-meli-one-by-one-sync
description: Publish or dry-run exactly one eligible Shopify product to Mercado Libre Chile using Zipp's product-publishing automation. Use whenever the user asks to sync, publish, validate, schedule, or gradually move Shopify products to MLC one at a time, including no-op runs and retrying the same partially created item. Do not use for bulk publishing, stock synchronization, or repairing an already-mapped inactive listing.
---

# Shopify -> Mercado Libre: One Product

Run the repository's guarded one-product workflow. Shopify is the source of
product truth; Mercado Libre is the destination. The primary goal is an active,
high-quality, source-faithful listing with a durable mapping. Each invocation ends with one
of five outcomes: 'no-op', 'validated', 'published', 'blocked', or
'failed_retryable'.

This skill controls a production marketplace. Default to dry-run only when the
user asks to validate, inspect, preview or test. An invocation that says
`Ejecuta la skill Shopify one by one sync` is an authorized production run:
publish exactly one eligible product after its mandatory local and remote
validation gates. It is suitable for a scheduler that invokes it every two
hours. When no eligible product exists, return `no-op` and make no writes.

## Scope

- Process exactly one eligible Shopify product, or no-op when none exists.
- A mapped Shopify product is not selected again; ongoing stock changes are
  handled by stock synchronization.
- One Shopify product publishes as one Mercado Libre item. When it has
  variants, that item contains every Shopify variant, including stock-zero
  variants.
- Resume the same journaled Mercado Libre item after a transient failure.
- Never choose a second product after selection, validation failure, creation,
  moderation, or blocking.
- Use 'inactivas' for an already-mapped inactive or policy-paused listing.
- Use the stock-sync automation for ongoing inventory updates. This workflow
  only uses current stock during initial publication.

## Setup

Run from `product-sync/`:

~~~bash
PY=../venv/bin/python
WORK_DIR=state/work
~~~

The canonical state files are:

- 'state/sync_mappings.json'
- 'state/sync_blocked_products.json'
- 'state/publication_journal.json'
- 'state/selection_reservations.json'
- 'state/publication_quality_records.json'

Do not substitute root-level legacy state files. The code merges the old
root-level blocker file only for backward compatibility.

Publish-mode preparation reserves the selected Shopify ID. A later invocation
must resume that reservation instead of selecting another product. The guarded
CLI also uses a process lock so overlapping cron executions cannot race.

## Workflow

1. Determine the invocation mode.

   - For validation/inspection/preview requests, use dry-run.
   - For `Ejecuta la skill Shopify one by one sync`, including a scheduled
     invocation, use production mode. Do not ask a second confirmation.

2. Prepare exactly one product:

   ~~~bash
   $PY scripts/one_by_one_sync.py prepare \
     --mode publish
   ~~~

   For a dry-run, substitute `--mode dry-run`. Dry-run reconciliation is
   read-only; publish mode may atomically remove mappings proven missing or
   deleted.

3. Read '$WORK_DIR/source.json'.

   - If 'selected_product' is null, report 'no-op' and stop.
   - Otherwise read [references/business-contract.md](references/business-contract.md)
     and [references/payload-guide.md](references/payload-guide.md) completely.
   - Read [references/quality-catalog.md](references/quality-catalog.md) before
     constructing the payload or evaluating post-live quality. If its last
     normative review is older than one month, or Meli returns an unknown
     signal, refresh it from current official Mercado Libre sources and record
     the new date and changed rule. Do not browse on every scheduled run.
   - Inspect the Shopify images before choosing their order or visible brand.
   - If 'policy_hints' is non-empty, or the product appears regulated,
     hazardous, counterfeit, medical, adult, or otherwise sensitive, also read
     [references/policy-preflight.md](references/policy-preflight.md) and check
     the current official Mercado Libre policy pages.

4. Establish the product's functional identity: what the item is, its primary
   function and which title words merely describe shape, portability or use.
   Treat Domain Discovery as a candidate generator. Do not select a category
   from a single matching word.

   Start with the candidates saved in `source.json`. If the prepared category
   is semantically wrong, discover another source-supported query and select
   one of the resulting candidates:

   ~~~bash
   $PY scripts/one_by_one_sync.py discover-category \
     --query "FUNCTIONAL PRODUCT QUERY" --reason "Shopify evidence"
   $PY scripts/one_by_one_sync.py select-category \
     --category-id MLC123 --reason "Type/function/attribute fit"
   ~~~

   Evaluate at most five unique categories. For every selection,
   explain why its type, function and required attributes fit the Shopify
   evidence. Never invent attributes to make a category fit.

5. Create '$WORK_DIR/productos_listos.json' as a JSON array containing exactly
   one payload. Derive facts only from 'source.json', the original Shopify
   images, category metadata, allowed values, and current official policy.
   Never call Gemini or another paid local AI API. For every Shopify variant,
   including variants with zero stock, its non-empty exact Shopify SKU is
   mandatory identity data: prepare it in that Meli variation's
   `seller_custom_field` and `SELLER_SKU` attribute.

6. Run the local contract gate:

   ~~~bash
   $PY scripts/one_by_one_sync.py check-payload
   ~~~

   Correct every reported violation in the same payload. If a required fact
   cannot be known truthfully, record a terminal blocker and stop:

   ~~~bash
   $PY scripts/one_by_one_sync.py block \
     --reason-type missing_data \
     --reason "Concrete missing fact and why it cannot be inferred safely"
   ~~~

7. Validate with Mercado Libre without publishing:

   ~~~bash
   $PY scripts/one_by_one_sync.py submit
   ~~~

   Read '$WORK_DIR/result.json'. If Mercado Libre rejects a fixable field,
   correct only that field and validate the same product again, with at most
   three attribute-correction rounds per category. If it rejects the initial
   title, try at most three source-faithful titles that preserve product
   identity. If validation
   proves a terminal product, policy, or image blocker, record it with the
   'block' command. Do not block authentication, network, rate-limit, or server
   failures.

   If Meli rejects the standard payload with `variations`, do not remove,
   aggregate or split the variants into separate listings. Correct a concrete,
   source-faithful validation error when possible; otherwise block the product.

8. For the autonomous production invocation, publish the same validated
   product immediately:

   ~~~bash
   $PY scripts/one_by_one_sync.py submit --publish
   ~~~

   Do not ask the user to approve this second command: the invocation itself
   is the authorization. The publisher journals the item ID immediately after creation, resumes that
   same ID after transient failures, uploads unchanged Shopify images when
   picture ingestion is pending, checks persisted attributes, and maps the
   product only after repeated clean live reads.

9. Complete the post-live quality pass before reporting success. Re-read the
   active item, patch supported missing attributes on that same item, verify
   free shipping and the final gallery, and save a quality record containing:
   achieved objectives, omitted objectives with reasons, Meli warnings, and
   whether a visual quality score was unavailable through the API. Optional
   goals requiring Premium, discounts, higher stock or invented facts do not
   block success. SKU identity is not optional: the live read must show the
   exact Shopify SKU in both `seller_custom_field` and `attributes.SELLER_SKU`.
   If either is absent or differs, patch the same item and re-read it. Do not
   save its mapping or report `published` until both fields persist.

10. If validation or live publication needs category-specific recovery, read
   [references/recovery-and-exceptions.md](references/recovery-and-exceptions.md)
   before changing the payload or item.

## Non-negotiable gates

- Preserve Shopify inventory and variant prices; never create artificial stock.
- **Fundamental SKU identity gate — cannot be bypassed:** Shopify SKU is the
  shared identity used to reconcile listings and synchronize stock. Every
  Shopify variant, including one with zero stock, must have one non-empty,
  exact SKU in its Meli variation's `seller_custom_field` and `SELLER_SKU`
  attribute. Never substitute a Shopify variant ID, title, model, generic
  value or a SKU from an older Meli item. If a SKU is absent, duplicated
  ambiguously, rejected, or fails to persist, do not create or map a
  successful listing: record the concrete blocker or retryable API failure and
  stop.
- Publish all Shopify variants in one Meli item. Each must retain its exact
  SKU, stock (including zero), price, faithful image and supported option
  combination. Never omit or split a variant into a separate Meli item.
- First use the exact Shopify title after removing every textual trace of Zipp.
  Try an alternative only after a real Meli rejection, preserving identity and
  using only source-supported terms.
- No buyer-facing Meli text, attribute or selected image may contain Zipp,
  Zipp Chile, Zipp.cl or an equivalent store mark.
- Use a physical manufacturer brand only when Shopify text or an original
  image proves it is printed on the sold item. A brand on a prop or background
  device is irrelevant. Otherwise use `Genérica`; Shopify vendor is never
  manufacturer evidence.
- Use only Shopify images. A visually different variant needs at least one
  matching Shopify image; non-visual variants may share faithful images. Block
  the product when any variant cannot be represented faithfully.
- If Meli actually rejects the only faithful cover for a white-background
  requirement, a controlled background-removal remediation is allowed. It may
  change only the background, must retain the original as a secondary image,
  and must record provenance. If fidelity is uncertain, block.
- Remove contact information from every buyer-facing field.
- Default to free shipping, but report the live shipping configuration because
  Mercado Libre may override the request.
- Fill every achievable required, catalog and recommended attribute supported
  by Shopify. An unknown value is omitted when optional; `No aplica` is used
  only when the concept truly does not apply and Meli accepts it. Never use a
  connector pair, generic product name, SKU or legacy value as a model, and
  never infer a cable data standard from connector shape.
- Build a faithful plain-text description from Shopify facts. Remove store
  copy, links, promotions and Zipp; do not invent warranty, accessories,
  certifications, compatibility or performance.
- A live run succeeds only with 'status: active', empty 'sub_status', empty
  'warnings', persisted expected attributes, every Shopify variation (including
  zero-stock variants) present with matching stock and price, its exact SKU
  persisted in `seller_custom_field` and `attributes.SELLER_SKU`, and a saved
  mapping.
- Before creating, reconcile the seller's own items by exact SKU. Adopt only a
  single fully faithful match. Multiple matches, approximate title/image
  matches, or an exact-SKU listing that is structurally incoherent are reported
  as a terminal blocker for human review; this skill does not repair old
  listings.
- When the agreed category/title/attribute attempt budget is exhausted, record
  every attempted route and the concrete reason, block the product, and end the
  invocation without selecting another product. Transient auth, network, 429
  and 5xx failures remain retryable and never consume that budget.

## Result handling

Treat '$WORK_DIR/result.json' as authoritative:

- 'published': success; report the Shopify ID and Meli ID,
  verification and live shipping block.
- 'validated': dry-run success; no listing or mapping was created.
- 'no-op' or 'already_synced': nothing was published.
- 'blocked': stop; do not select another product.
- 'failed_retryable': keep the product unblocked. If 'resume_same_item' is true,
  the next authorized run must resume the journaled Meli ID.
- 'validation_failed': inspect the exact causes, safely correct the same
  payload, or record a concrete terminal blocker.

Keep the final response short and operational. Include 'published',
'shopify_id', 'meli_id' when present, 'mode', 'outcome', 'verification' for a
live success, and 'reason' for any non-success.
