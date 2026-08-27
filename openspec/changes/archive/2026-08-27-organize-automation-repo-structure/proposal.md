## Why

Zipp is becoming a general automation repository, not only a Shopify / Mercado Libre stock synchronizer. The current root-level mix of scripts, handoff notes, context files, tests, and generated artifacts makes the first automation harder to hand off and will become more confusing as more automations are added.

This change establishes a clear repository hierarchy where each automation has its own folder, documentation, scripts, tests, and operational notes. The existing stock synchronizer becomes the first concrete automation under that structure, with documentation written from the implemented behavior rather than relying on scattered or historically incomplete notes.

## What Changes

- Introduce a top-level `automations/` directory as the home for concrete Zipp automations.
- Create an `automations/stock-sync/` area for the existing Shopify <-> Mercado Libre stock synchronization automation.
- Move or consolidate stock-sync documentation into the stock-sync automation folder.
- Keep root documentation focused on the repository as a whole: purpose, conventions, setup overview, and index of automations.
- Add a repeatable documentation pattern for future automations.
- Separate shared integration clients and reusable helpers from automation-specific scripts where practical.
- Add operations-oriented documentation for server handoff: environment variables, secrets, webhooks, dry-run/apply commands, SQLite inspection, logs, backups, and known failure states.
- Preserve existing working behavior while updating commands and paths affected by the reorganization.

## Capabilities

### New Capabilities

- `automation-repo-structure`: Defines how this repository organizes multiple Zipp automations, shared code, operations files, runtime data, and documentation.
- `stock-sync-handoff-documentation`: Defines the documentation contract for the existing stock synchronization automation, including handoff, architecture, operations, troubleshooting, and pending work.

### Modified Capabilities

- None.

## Impact

- Affected documentation: root `README.md`, existing stock sync notes, and new automation-local documentation under `automations/stock-sync/`.
- Affected code paths: existing stock sync scripts may move under `automations/stock-sync/scripts/`, requiring command updates and import/path checks.
- Affected tests: stock sync tests may move under `automations/stock-sync/tests/`, requiring updated test commands.
- Affected operational setup: server commands, cron/systemd examples, webhook catcher paths, and SQLite/log locations must be documented consistently.
- External systems: Shopify, Mercado Libre, GitHub, and the future production server are described operationally, but their API behavior is not changed by this proposal.
