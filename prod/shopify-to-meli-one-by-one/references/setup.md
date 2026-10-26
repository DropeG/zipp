# Production setup

This is an agent-driven scheduled workflow, not a daemon. Perform these steps
once before enabling its two-hour schedule.

## 1. Install the runtime

From this folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Keep `.venv`, `.env`, `data/state.json`, and `work/` on persistent local
storage. Never commit them.

## 2. Configure credentials

The script first reads this folder's `.env`, then fills missing variables from
`PRODUCT_SYNC_SHARED_ENV_FILE` when configured, otherwise from
`../stock-sync/.env`. Copy `.env.example` to `.env` and supply the Shopify and
Mercado Libre values that are not already available from stock-sync. An
absolute shared environment path is useful when this Paseo workspace is a Git
worktree and the ignored stock-sync `.env` exists only in the main checkout.

Reuse the stock-sync Mercado Libre token file; do not create a second rotating
refresh-token chain. Keep the default shared stock-worker lock so publication,
token refresh, and inventory reconciliation cannot run concurrently.

Required runtime files are:

- a populated `.env` here or in `../stock-sync/.env`;
- the Mercado Libre token JSON at the configured path; and
- writable `data/` and `work/` directories.

## 3. Discover listings that are already synced

First generate a read-only preview from the live Shopify and Mercado Libre
accounts:

```bash
.venv/bin/python scripts/meli.py bootstrap
```

Inspect `work/bootstrap.json`. The scan includes all Shopify product statuses
and all seller items returned by Mercado Libre's scan search. It extracts SKUs
from `seller_custom_field`, `SELLER_SKU`, and legacy variations. It maps only a
SKU that occurs exactly once in Shopify and exactly once in an active or paused
Mercado Libre listing owned by the configured seller.

The report separates verified mappings, ambiguous SKUs, missing SKUs,
conflicting SKU fields, unmatched SKUs, unsupported listing shapes, and
inactive/closed items. The preview writes only the report; it does not change
the state or any listing.

An active or paused Mercado Libre item with an unreadable or missing SKU is an
application blocker because it cannot be proved distinct from a Shopify
product. Resolve those item IDs before applying bootstrap.

After reviewing the report, atomically add the verified mappings to local state:

```bash
.venv/bin/python scripts/meli.py bootstrap --apply
```

Applying bootstrap still performs no listing creation or update. Existing local
mappings are preserved, locally blocked products are not remapped, and a
pending publication prevents application. It also refuses to apply if a scanned
item disappeared before verification or a current listing cannot be identified
by SKU. The command is safe to repeat. A
production `next` refuses to run until the applied-bootstrap marker exists.
Products involved in an ambiguity or unsafe listing relationship are recorded
in `bootstrap.held_product_ids` and are not mapped; scheduled runs skip them
while continuing with unaffected products. Resolve those cases and apply
bootstrap again before they can enter the publication queue.

Bootstrap is only the initial snapshot. Every `next` and `publish` also reads a
lightweight, paginated Shopify variant/SKU snapshot. If a SKU is reused after
bootstrap or a mapped variant changes SKU, the affected product is held and no
listing is created or adopted.

The historical `product-sync/state/sync_mappings.json` file is not imported and
is not authoritative; it may be used manually only to investigate a reported
discrepancy.

## 4. Test without creating

Invoke this skill manually with an explicit dry-run request. Confirm that the
agent reaches a `validated` result and inspect `work/payload.json` and
`work/result.json`. Dry-run reads live Shopify and Mercado Libre data and calls
Mercado Libre's validator, but it does not create listings or update migration
state.

## 5. Create the schedule

Run `scripts/dispatch.py` from the operating-system crontab every two hours.
The dispatcher calls `meli.py next` first. When the result is `no-op`, it exits
without creating a chat. When a product is prepared, it creates a new local
workspace rooted at the Git folder and names that workspace after the Shopify
product before starting one background Paseo agent there. When preflight fails,
it creates a clearly named error workspace and chat that reports the failure
without publishing. At the end of the chat, successful workspace names remain
plain, retryable runs gain an `[R]` prefix, and terminal failures, validation
failures, or review-required runs gain an `[F]` prefix. The next dispatcher run
also applies this rename as a fallback if the agent did not. It records the
workspace and agent IDs and will not create a duplicate while that agent is
still running.

A representative entry is:

```cron
0 */2 * * * cd /git && /usr/bin/python3 prod/shopify-to-meli-one-by-one/scripts/dispatch.py --cwd /git --provider codex/MODEL --thinking THINKING --mode full-access >> prod/shopify-to-meli-one-by-one/data/dispatch.log 2>&1
```

Use a local workspace rooted at the same Git folder, not a fresh worktree,
because `data/state.json` must persist between runs. The Paseo daemon and this
machine must remain running. Install the cron entry only after bootstrap has
been applied.

## 6. Operate and recover

Check local state without API calls:

```bash
.venv/bin/python scripts/meli.py status
```

Back up `data/state.json`. An interrupted product remains pending and is
resumed on the next run. If the process stopped during `POST /items`, the tool
refuses to create again until exact-SKU reconciliation finds the first result;
inspect Mercado Libre manually before editing that pending state.
