# Payload Guide

Read this reference whenever 'prepare' selects a product.

## Evidence order

Use evidence in this order:

1. Shopify title, description, options, variants, barcode and original images.
2. Visible facts in the original Shopify images.
3. Mercado Libre category attributes and allowed values in 'source.json'.
4. A truthful business default explicitly allowed below.

Shopify `vendor` identifies the seller in the storefront; it is never evidence
that the seller manufactured the item. Use a non-generic Meli brand only when
Shopify text or an original image proves that manufacturer brand appears on the
sold item. Ignore brands on props and background devices. Otherwise use
`Genérica`. Never send Zipp, Zipp Chile, Zipp.cl or an equivalent store trace in
text, attributes or selected images. Do not infer certifications,
registrations, dimensions, wattage, materials, cable length, compatibility or
model numbers.

## Required shape

'productos_listos.json' is an array with one object containing:

~~~json
{
  "shopify_id": "123",
  "category_id": "MLC...",
  "original_title": "Exact Shopify title",
  "price": 12990,
  "stock": 2,
  "barcode": "No aplica",
  "images": ["unchanged Shopify URL"],
  "shipping": {
    "mode": "me2",
    "local_pick_up": true,
    "free_shipping": true
  },
  "variations": [],
  "ai_data": {
    "optimized_title": "Exact Shopify title",
    "clean_description": "Buyer-facing plain text",
    "brand": "Visible truthful brand",
    "model": "Explicit manufacturer model or No aplica"
  },
  "extra_attributes": []
}
~~~

## Title and description

Keep 'optimized_title' identical to the Shopify title after mandatory removal
of Zipp/store trace. Other changes require an actual Meli rejection and one of
the reasons in 'business-contract.md'. Try no more than three faithful title
alternatives.

Write plain Spanish buyer-facing text. Preserve Shopify facts while removing
HTML, URLs, contact instructions, promotions, store conditions, internal stock
details, unavailable-color diagnostics, Shopify, SKU, variant IDs, API behavior
and validation notes. It may reorganize facts into product, characteristics,
compatibility and package contents, but must not invent warranties, accessories,
certifications, compatibility or performance.

Do not normalize `Zipp Chile`/`Zipp.cl` into the Mercado Libre brand. It may
remain the Shopify vendor while the Meli brand is `Genérica`.

## Price, stock and variants

Choose top-level price from an in-stock variant with a positive price. Never set
top-level stock above the sum of Shopify inventory.

For a multi-variant product, keep every Shopify variant in local source context,
but publish only variants with current positive stock and price:

- include every Shopify variant exactly once in the prepared contract so omitted
  options can be audited; mark stock-zero options unpublished;
- preserve each variant's Shopify ID, SKU, barcode, price and inventory;
- map option names to a category-supported variation attribute such as
  'COLOR', 'SIZE' or 'CAPACITY';
- use only image URLs present in the selected Shopify product.

If Mercado Libre validation requires User Products and rejects `variations`
with `family_name`, keep the prepared per-variant data intact. The guarded
publisher must switch automatically to `user_products_family`:

- create one Meli item for each sellable, evidence-complete Shopify variant;
- give every child its exact Shopify SKU, price, stock and assigned Shopify
  image;
- copy its supported option combination (for example color, size, capacity or
  length) into the child attributes;
- use the same `family_name` for all children;
- validate every child before creating any of them;
- exclude stock-zero variants and record them as `unpublished_variants`;
- record an in-stock variant without sufficient visual evidence as
  `pending_evidence`; publish other faithful variants and block the whole
  product only when none remains;
- persist a structured mapping by Shopify variant ID.

Never remove `variations`, sum inventory, or use `variations_fallback_reason`
to publish multiple sellable Shopify variants as one Meli item. If a sellable
variant lacks a SKU or unambiguous supported option combination, exclude and
record it. A visually distinct color/design needs a matching image; a
non-visual option such as length or capacity may share a faithful product image.
Stop only when no sellable variant remains publishable.

## Characteristics

Use 'category_attributes' from 'source.json'.

- Fill every 'required', 'catalog_required', and 'required_for_catalog'
  attribute that is editable.
- Use exact 'value_id' values when an allowed value matches.
- Fill recommended secondary attributes when the source proves the value.
- Use 'No aplica' only when the characteristic truly does not correspond to the
  product and Meli accepts that response. Unknown is not the same as not
  applicable.
- Leave read-only, certification, regulatory and unknown fields empty.
- Omit optional unknown model-like fields. When the product genuinely has no
  manufacturer model and Meli accepts `No aplica`, record that semantic reason.
  A product
  type, title fragment, connector pair, SKU, or an old Meli value is not model
  evidence. When a real model is used, add 'model_evidence' with its Shopify
  source and exact supporting quote.
- Set protocol/type fields such as 'DATA_CABLE_TYPE' to 'No aplica' unless the
  Shopify source explicitly states the standard (for example USB 2.0). Never
  infer a data standard from the connector shape.
- Use a real numeric barcode when Shopify provides one. Otherwise set the
  top-level 'barcode' to 'No aplica'; the publisher applies the validated empty
  identifier strategy.

For 'IS_KIT', use 'No' only when the product is clearly a single item. Do not
use generic defaults for voltage, manufacturer, MPN or compatibility without
source evidence.

## Shipping and images

Set 'free_shipping: true' by default. Use 'me2' for normal products when
supported. A 'policy_hint' is a prompt to investigate, not proof that
'not_specified' is required.

Build a Meli-eligible subset of Shopify images and order it as:

1. cleanest product-focused cover;
2. useful context image;
3. useful detail image;
4. remaining distinct source images.

Exclude any image containing Zipp/store/contact marks or showing a different
variant as the sold option. Do not fetch replacement photos. If Meli actually
rejects the only faithful cover for a category-specific white-background rule,
background removal is permitted as a controlled remediation: change only the
background, retain the original as a secondary image and record source URL,
checksum, transformation and output checksum. Never reconstruct, recolor or
alter product pixels.
