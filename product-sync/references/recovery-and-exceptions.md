# Recovery and Confirmed Exceptions

Read this reference only after Mercado Libre validation or a live item reports
an error, warning, sub-status or moderation state.

## Recovery rules

- 'picture_download_pending': upload each original Shopify file unchanged to
  Mercado Libre picture hosting, update the same item and re-read it.
- 'waiting_for_patch': inspect the exact moderation reason and correct only the
  same item. Do not blindly reactivate or create a replacement.
- Contact-data moderation: remove contact data from every editable buyer-facing
  field. Shopify vendor is not a Meli brand; preserve the configured
  `zipp_sync.meli_brand` or the safe default `Genérica`.
- Brand mismatch: use the brand visibly printed on the product when it is clear
  and truthful.
- Network, auth, 429 and 5xx failures: preserve the journal and return
  'failed_retryable'.
- Deleted journaled item: record the item ID and block automatic replacement
  until a user approves a retry.

The live gate requires at least two consecutive reads with:

~~~json
{"status": "active", "sub_status": [], "warnings": []}
~~~

After that, compare expected payload attributes with live attributes, patch
missing supported values on the same item once, and run the gate again.

Then write a quality record listing achieved controls and every surfaced goal
that was omitted with a reason. Missing Premium, discounts, extra stock or an
API-exposed visual score does not fail an otherwise faithful clean-active item.

## API-format recoveries

These are observed API-format recoveries, not product-category selection rules:

- For charger categories 'MLC157684' and 'MLC159239', if and only if Shopify has
  no numeric GTIN, 'EMPTY_GTIN_REASON' is rejected with
  'item.attribute.missing_conditional_required', and a second '/items/validate'
  accepts 'GTIN: No aplica', the publisher may use that validated fallback.
- If User Products rejects `variations` with `family_name`, preserve all
  variant data and let the guarded publisher validate/create one User Product
  child per sellable Shopify variant. Aggregated-stock fallback and publishing
  only the first option are both forbidden.

Mercado Libre may normalize equivalent units or override requested shipping.
Trust and report the final live item, provided it still passes all gates.
