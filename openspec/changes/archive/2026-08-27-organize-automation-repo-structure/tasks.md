## 1. Inventory and Classification

- [x] 1.1 Review all tracked root-level files and classify them as repository-global, stock-sync-specific, shared integration code, tests, or legacy/historical notes.
- [x] 1.2 Identify ignored runtime files that must stay out of Git and confirm `.gitignore` still covers secrets, tokens, SQLite databases, logs, generated reports, and temporary assets.
- [x] 1.3 Decide whether `sync_products.py` belongs in `stock-sync` for this change or should remain/root-move later as a separate product publishing automation.
- [x] 1.4 Decide whether Excel stock/reporting utilities belong in `stock-sync`, a future reporting automation, or a temporary legacy area.

## 2. Repository Structure

- [x] 2.1 Create the top-level `automations/` folder.
- [x] 2.2 Create `automations/stock-sync/` with `docs/`, `scripts/`, and `tests/` subfolders.
- [x] 2.3 Create a shared helper area for reusable Shopify, Mercado Libre, and AI clients if those files are moved in this change.
- [x] 2.4 Create repository-level docs for general structure and server baseline if needed.

## 3. Stock Sync Documentation

- [x] 3.1 Write `automations/stock-sync/README.md` as the canonical entry point for the synchronizer.
- [x] 3.2 Write `automations/stock-sync/docs/HANDOFF.md` with server setup, credentials, OAuth, webhook setup, dry-run, apply, and launch checklist.
- [x] 3.3 Write `automations/stock-sync/docs/ARCHITECTURE.md` explaining Shopify -> Mercado Libre and Mercado Libre -> Shopify flows, SQLite tables, task ids, and safety rules.
- [x] 3.4 Write `automations/stock-sync/docs/OPERATIONS.md` with recurring commands, logs, SQLite inspection queries, backups, and normal operating rhythm.
- [x] 3.5 Write `automations/stock-sync/docs/TROUBLESHOOTING.md` with common permission, SKU, duplicate, inventory, token, webhook, and retry states.
- [x] 3.6 Write `automations/stock-sync/docs/PENDING.md` distinguishing production hardening, future improvements, and out-of-scope V1 items.
- [x] 3.7 Replace or retire root-level stock-sync notes after their validated content is migrated into canonical automation-local docs.

## 4. Script and Import Reorganization

- [x] 4.1 Move stock-sync-specific webhook and processor scripts into `automations/stock-sync/scripts/`.
- [x] 4.2 Move stock-sync tests into `automations/stock-sync/tests/`.
- [x] 4.3 Update Python path resolution so moved scripts still read repository-level `.env`, `meli_tokens.json`, `sync_mappings.json`, and `data/stock_sync.db` intentionally.
- [x] 4.4 Update Node path resolution so the webhook catcher writes to the intended repository-level `data/stock_sync.db`.
- [x] 4.5 Update imports if reusable clients move into a shared helper area.
- [x] 4.6 Update all documented commands to use the new canonical paths.

## 5. Root Documentation

- [x] 5.1 Rewrite root `README.md` to describe the repository as Zipp's general automation hub.
- [x] 5.2 Add an automation index that links to `automations/stock-sync/` and explains the pattern for future automations.
- [x] 5.3 Document which files and folders are global, automation-local, shared, operations-related, runtime-only, or ignored.
- [x] 5.4 Keep root setup instructions short and point detailed stock-sync setup to automation-local documentation.

## 6. Verification

- [x] 6.1 Run Git status checks to ensure no secrets, tokens, databases, logs, generated reports, or temporary assets are staged.
- [x] 6.2 Run stock-sync unit tests after moving files.
- [x] 6.3 Run command help or dry-run smoke checks for the stock-sync processors from the new paths.
- [x] 6.4 Verify documented SQLite paths, webhook endpoints, cron examples, and systemd examples match the reorganized file layout.
- [x] 6.5 Validate OpenSpec artifacts for this change.
