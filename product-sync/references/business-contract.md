# Business Contract

Read this reference whenever 'prepare' selects a product.

## Precedence

When two rules conflict, use this order:

1. Mercado Libre policy and truthful product representation.
2. No Zipp/store trace in Mercado Libre content.
3. No duplicates and recovery of an already-created item.
4. Exact Shopify inventory, prices, variants, and evidence.
5. Mercado Libre validation and category requirements.
6. Shopify-exact title.
7. Listing-quality improvements.

Consequences:

- If a Shopify title contains contact data, remove only the prohibited portion
  and set 'title_change_reason' to 'contact_policy'.
- If it contains Zipp, Zipp Chile, Zipp.cl or an equivalent store mark, remove
  only that trace before the first attempt and set 'title_change_reason' to
  'store_brand_policy'.
- If Mercado Libre rejects the exact title solely because of a hard length
  limit, shorten it minimally and set 'title_change_reason' to
  'meli_title_limit'.
- Never inflate inventory or invent a technical characteristic to improve
  listing quality.

## Eligibility

A product is eligible when:

- its Shopify ID is absent from the reconciled canonical mapping, or its
  structured family mapping lacks a currently sellable Shopify variant;
- it is not terminally blocked;
- Shopify reports it as active;
- at least one individual variant has both positive stock and positive price;
- it has at least one Shopify image.

The selector reads all Shopify pages and chooses the first eligible product in
the API order. Zero-stock variants remain source context but are not published.

Reconciliation removes a mapping only when Mercado Libre positively returns
404, 'status: deleted', or a 'deleted' sub-status. Permission, network,
authentication and server failures do not prove deletion.

## Category selection

Treat Domain Discovery as a candidate generator, not unquestionable product
truth. Establish the product's functional identity from Shopify title,
description, type, options and original images. Evaluate no more than five
unique candidates using product-type fit, primary-function fit, category
hierarchy, required attributes and non-publishing validation.

Never select a category because of one incidental title word and never satisfy
an unrelated category by inventing attributes. Record the queries, candidates,
selection rationale and rejection reason. Allow up to three attribute
correction rounds per category.

## Existing-item reconciliation

Before any create call, search the seller's own Mercado Libre items for every
sellable Shopify SKU. A unique exact-SKU result may be adopted only after its
identity, variant, category, price, stock and images are verified as faithful.
Run the normal quality pass before saving its mapping. Never adopt by title or
image similarity alone.

Multiple SKU matches, an approximate match, or an exact-SKU item that is
structurally incoherent is a terminal human-review blocker. Report it and stop;
do not repair, pause, replace or delete that old listing in this skill.

## State and idempotence

- 'sync_mappings.json' records completed active publications. Legacy entries are
  scalar Shopify-product-to-Meli-item IDs; User Products entries are structured
  families with a mapping for each Shopify variant.
- A scalar mapping is forbidden when Meli rejected legacy variations and the
  Shopify product has multiple sellable variants. In that case completion
  requires a structured `user_products_family` mapping with one child entry per
  sellable Shopify variant. Stock may never be aggregated across those entries.
- 'sync_blocked_products.json' records terminal product/data/policy failures.
- 'publication_journal.json' records a created item before later steps run.
- 'selection_reservations.json' prevents overlapping runs from selecting a
  second product before the first is published or terminally blocked.

Once an item ID is journaled, resume and correct the same item after transient
failure. A newly created zero-sale item may undergo a controlled replacement
only when an immutable category, title or variant-structure error is proven;
the replacement must validate and become clean-active before the old item is
closed. Pre-existing items found during reconciliation are never replaced here.

A publish-mode reservation survives validation corrections and transient
failures. Release it only after a successful mapping or a concrete terminal
blocker. Use the guarded CLI so process locking and reservation checks apply.

Write state atomically. Dry-run must not change mappings, blockers, journals or
Mercado Libre listings.

## Terminal outcomes

- 'no-op': no eligible product exists.
- 'validated': exactly one payload passed local and Mercado Libre validation;
  no listing was created.
- 'published': exactly one new item, or one complete User Products family for
  the selected Shopify product, passed the complete live gate and was mapped.
- 'blocked': evidence proves a terminal policy, missing-data, image,
  validation, or moderation problem.
- 'failed_retryable': the environment or API failed transiently. Preserve any
  journal and do not block or replace the product.

After five category candidates, three corrections per category and three
source-faithful rejected-title alternatives are exhausted, persist the full
attempt log as 'blocked' and end the invocation. Never select a second product
in the same run. Later runs skip the persistent blocker.
