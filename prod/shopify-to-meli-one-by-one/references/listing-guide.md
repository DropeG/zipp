# Listing guide

Read this after `next` returns a product and before writing `work/payload.json`.

## Agent payload

Write one JSON object with this shape:

```json
{
  "shopify_id": "gid://shopify/Product/123",
  "category_id": "MLC123",
  "title": "Legacy-model title",
  "family_name": "User Products family name",
  "description": "Plain Spanish description",
  "brand": "Genérica",
  "model": "No aplica",
  "shipping_mode": "me2",
  "pictures": ["https://cdn.shopify.com/..."],
  "attributes": [
    {"id": "MATERIAL", "value_name": "Aluminio"}
  ],
  "variant_attributes": {
    "gid://shopify/ProductVariant/456": [
      {"id": "COLOR", "value_name": "Negro"}
    ]
  },
  "variant_pictures": {
    "gid://shopify/ProductVariant/456": ["https://cdn.shopify.com/..."]
  }
}
```

Use `title` for `legacy_variations`; use `family_name` for
`user_products_family`. The unused field may be omitted. Do not include SKU,
price, stock, barcode, condition, listing type, or seller IDs: the script
copies or derives them from `source.json` and configuration.

`shipping_mode` is optional and defaults to `me2`. Use `not_specified` only
when the product/category is not eligible for Mercado Envíos, such as a
shipping-restricted product confirmed by current Mercado Libre rules. The
script always requests local pickup and free shipping; do not infer a shipping
restriction from a vague title alone.

## Evidence rules

- Derive product facts from Shopify text, options, variants, barcodes, and
  original images. Category metadata may constrain values but is not product
  evidence.
- Use `Genérica` unless Shopify text or the sold product itself visibly proves
  a physical manufacturer brand. Shopify vendor is not sufficient evidence.
- Use `No aplica` for model only when the product truly has no manufacturer
  model and Mercado Libre accepts it. Never substitute the SKU or a generic
  product type as a model.
- Use the initial Mercado Libre category candidate only when its product type
  and function fit. Use `category --query` and `category --id` when another
  category is needed.
- Fill required category attributes when Shopify proves their values. Omit
  unknown optional attributes; never guess certifications, compatibility,
  dimensions, materials, performance, or technical standards.

## Buyer-facing content

- Write clear Spanish text. The title or family name should identify the
  product without promotions, stock claims, contact information, or store
  branding.
- Remove HTML, URLs, social/contact instructions, promotions, internal notes,
  Shopify references, and Zipp references from the description. Do not invent
  warranty, accessories, certifications, compatibility, or performance.
- Use only URLs listed in `source.json`. Order the cleanest product-focused
  image first, then useful context and detail images. Do not edit, generate, or
  fetch replacement images in this skill.
- For visually different User Product variants, provide matching
  `variant_pictures`. Non-visual variants may share a faithful product image.

## Variants

`variant_attributes` maps each Shopify variant ID to its Mercado Libre option
attributes. Use category-supported IDs such as `COLOR`, `SIZE`, or `CAPACITY`.
Every multi-variant product needs a unique, source-faithful combination.

The script publishes every legacy variant, including zero-stock variants. In
User Products mode it publishes each currently sellable variant as a separate
item with the same family name and defers non-sellable variants until a later
run.
