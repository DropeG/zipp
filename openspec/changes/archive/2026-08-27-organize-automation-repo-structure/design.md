## Context

The repository currently contains the first Zipp automation: a local Shopify <-> Mercado Libre stock synchronizer. It works end-to-end locally, but the implementation and documentation were created over several weeks in an exploratory way. Root-level files now mix production scripts, helper clients, tests, historical notes, generated reports, and handoff material.

The repository is expected to host future Zipp automations, so its structure should not present the stock synchronizer as the whole project. The immediate stakeholder is the person who will mount the stock synchronizer on a server. Future stakeholders include anyone adding or operating other Zipp automations.

## Goals / Non-Goals

**Goals:**

- Make the repository read as a general Zipp automation hub.
- Give each automation a self-contained folder for its scripts, tests, documentation, and operational notes.
- Make `automations/stock-sync/` the canonical home for the existing Shopify <-> Mercado Libre stock synchronization automation.
- Produce trustworthy stock-sync documentation from implemented behavior and validated local flows.
- Keep server setup commands explicit and copyable.
- Preserve or deliberately update working commands when files move.
- Establish a repeatable folder/documentation pattern for future automations.

**Non-Goals:**

- Do not change stock synchronization business behavior.
- Do not add new sync features such as Shopify mirror orders, cancellation reversal, reconciliation, alerts, or webhook signature validation in this change.
- Do not deploy to the server.
- Do not publish new Mercado Libre listings as part of this change.
- Do not migrate runtime secrets or production data into Git.

## Decisions

### Use `automations/<automation-id>/` as the canonical unit

Each concrete automation will live under `automations/` with a stable kebab-case id.

Proposed shape:

```text
automations/
  stock-sync/
    README.md
    docs/
      HANDOFF.md
      ARCHITECTURE.md
      OPERATIONS.md
      TROUBLESHOOTING.md
      PENDING.md
    scripts/
    tests/
```

Rationale: an automation-local folder makes ownership obvious and prevents the repository root from accumulating unrelated scripts and notes.

Alternative considered: keep all scripts in root and only create docs folders. This is lower risk short term, but it leaves the repo structure misleading for future automations.

### Keep the root README general

The root `README.md` will describe the repository as the home of Zipp automations, list available automations, and point to each automation's local README and handoff docs.

Rationale: new readers should first understand the whole repository, then drill into a specific automation.

Alternative considered: keep the current stock-sync README at root. That works for today's handoff, but it makes future automations feel secondary or bolted on.

### Treat stock-sync documentation as automation-local product documentation

The stock synchronizer will get focused docs:

- `README.md`: what the automation is, current status, quick commands.
- `docs/HANDOFF.md`: server handoff checklist and setup sequence.
- `docs/ARCHITECTURE.md`: flows, SQLite queue, task states, safety rules.
- `docs/OPERATIONS.md`: recurring commands, dry-run/apply, logs, database inspection, backup.
- `docs/TROUBLESHOOTING.md`: common errors and recovery paths.
- `docs/PENDING.md`: known gaps and future improvements.

Rationale: handoff, architecture, operations, troubleshooting, and backlog serve different readers and moments. Splitting them keeps the handoff practical without losing depth.

Alternative considered: one very long stock-sync document. Easier to create, but harder to use during production incidents.

### Move scripts carefully and centralize path resolution

If implementation moves scripts from root-level `scripts/` into `automations/stock-sync/scripts/`, code must stop assuming that the repository root is one parent above the script file.

Path-sensitive files include:

- `.env`
- `meli_tokens.json`
- `sync_mappings.json`
- `data/stock_sync.db`
- `logs/`

The implementation should introduce a small path convention, either a shared helper or explicit `REPO_ROOT` discovery, so moved scripts still read and write runtime files in the intended repository-level locations.

Rationale: moving files without updating path assumptions can silently create a second database or fail to load credentials.

Alternative considered: leave stock-sync scripts in the root `scripts/` folder forever. This avoids path changes but weakens the automation hierarchy.

### Separate shared integrations from automation-specific workflows

Shopify, Mercado Libre, and AI helper clients are candidates for a shared area because future automations may reuse them. Automation-specific workflows should stay under the automation folder.

Candidate shared structure:

```text
shared/
  shopify_client.py
  meli_client.py
  ai_client.py
```

Rationale: shared clients represent integrations; stock-sync processors represent one workflow.

Alternative considered: copy helpers into each automation. That keeps automations isolated but creates credential and API behavior drift.

### Keep runtime state out of Git

Runtime files remain local/server-only:

- `.env`
- `meli_tokens*.json`
- `data/*.db`
- logs
- generated audits/reports
- temporary image folders

Rationale: the repository should contain source, docs, examples, and templates, not credentials or production state.

Alternative considered: commit sample databases or real logs for handoff. That risks leaking data and creates stale operational examples.

## Risks / Trade-offs

- Moving scripts can break imports or runtime paths -> Mitigate with focused path updates, test commands, and updated docs before server handoff.
- Too much restructuring before deployment can delay the server setup -> Mitigate by limiting this change to organization/documentation and behavior-preserving path fixes.
- Splitting documentation across several files can create duplication -> Mitigate with a short automation README that links to detailed docs instead of repeating them.
- Future automations may need different tech stacks -> Mitigate by defining a lightweight folder/documentation convention rather than a heavy framework.
- Existing historical notes may contain useful context but confusing wording -> Mitigate by moving validated facts into canonical docs and preserving historical notes only as secondary context if needed.

## Migration Plan

1. Create the new repository-level documentation structure.
2. Create `automations/stock-sync/` with docs, scripts, and tests subfolders.
3. Move or consolidate stock-sync documentation into automation-local docs.
4. Move stock-sync-specific scripts/tests into the automation folder if path updates are included in the implementation.
5. Move reusable clients into `shared/` only if imports can be updated and verified in the same change.
6. Update root README to point readers to the stock-sync automation and future automation convention.
7. Update all documented commands to use the new paths.
8. Run focused tests and at least dry-run command help checks to verify import/path behavior.
9. Commit the organization change after confirming ignored runtime files remain untracked.

Rollback strategy: because this is a repository organization change, rollback is a Git revert. If script movement causes deployment pressure, keep compatibility wrappers or temporarily document the old commands until the path fixes are verified.

## Open Questions

- Should the first implementation move only documentation, or move scripts/tests too?
- Should `sync_products.py` be part of `stock-sync`, or should product publishing become a separate automation such as `product-publishing`?
- Should inventory Excel tools be part of `stock-sync`, a separate `inventory-reports` automation, or remain in a temporary root area until clarified?
- Should shared clients move now, or stay at root until a second automation needs them?
