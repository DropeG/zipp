# stock-sync-handoff-documentation Specification

## Purpose
TBD - created by archiving change organize-automation-repo-structure. Update Purpose after archive.
## Requirements
### Requirement: Stock sync has automation-local documentation
The stock synchronization automation SHALL have canonical documentation inside `automations/stock-sync/`.

#### Scenario: Reader needs stock sync documentation
- **WHEN** a reader needs to understand the Shopify <-> Mercado Libre stock synchronizer
- **THEN** the root README points them to `automations/stock-sync/README.md`

#### Scenario: Documentation is specific to stock sync
- **WHEN** a document explains stock-sync behavior, setup, operation, or pending work
- **THEN** it is stored under `automations/stock-sync/` or its `docs/` subfolder

### Requirement: Stock sync handoff is server-oriented
The stock-sync handoff documentation SHALL provide a server setup path that an operator can follow without relying on undocumented local history.

#### Scenario: Operator prepares server
- **WHEN** the operator reads the handoff guide
- **THEN** they see prerequisites, clone/setup commands, `.env` creation, Mercado Libre authentication, webhook configuration, and service/cron guidance

#### Scenario: Operator validates before applying changes
- **WHEN** the operator is ready to test the synchronizer
- **THEN** the documentation explains dry-run commands before any apply commands

### Requirement: Stock sync architecture is explicit
The stock-sync architecture documentation SHALL explain both Shopify -> Mercado Libre and Mercado Libre -> Shopify flows using implemented behavior.

#### Scenario: Reader studies Shopify to Mercado Libre flow
- **WHEN** a reader reviews the architecture documentation
- **THEN** they can trace a Shopify order webhook through SQLite task creation, dry-run, apply, and synced status

#### Scenario: Reader studies Mercado Libre to Shopify flow
- **WHEN** a reader reviews the architecture documentation
- **THEN** they can trace a Mercado Libre order notification through order lookup, SQLite task creation, dry-run, apply, and synced status

### Requirement: Stock sync operations are documented
The stock-sync operations documentation SHALL explain recurring commands, task states, logs, SQLite inspection, backups, and safe recovery behavior.

#### Scenario: Operator checks queue health
- **WHEN** the operator wants to inspect current sync state
- **THEN** the documentation provides SQLite queries for task counts, recent tasks, raw events, and sync logs

#### Scenario: Operator sees reviewable tasks
- **WHEN** tasks are marked `needs_review` or `retryable_error`
- **THEN** the documentation explains that stock is not automatically changed and describes the next diagnostic step

### Requirement: Known gaps and future work are explicit
The stock-sync documentation SHALL distinguish validated behavior from pending production hardening and future improvements.

#### Scenario: Reader evaluates readiness
- **WHEN** a reader checks production readiness
- **THEN** they can see which parts are validated locally and which parts still need server setup, monitoring, backups, alerts, security hardening, or reconciliation

#### Scenario: Future automation work is planned
- **WHEN** a future improvement such as Shopify mirror orders or cancellation handling is considered
- **THEN** it is listed as pending/future work rather than implied to exist

### Requirement: Stock sync local testing is documented
The stock synchronization automation SHALL provide a local testing guide for humans at `automations/stock-sync/docs/LOCAL_TESTING.md`.

#### Scenario: Human tests stock sync locally with real webhook delivery
- **WHEN** a human wants to test the stock synchronizer locally while imitating production
- **THEN** the local testing guide explains how to start the webhook catcher, expose it through a temporary public tunnel, configure temporary webhook URLs, generate test events, inspect SQLite, run dry-run, and intentionally apply a limited real change

#### Scenario: Human avoids accidental stock changes
- **WHEN** the local testing guide describes applying changes
- **THEN** it requires dry-run review first and limits apply examples to controlled manual execution rather than automatic background processing

#### Scenario: Reader finds local testing guide
- **WHEN** a reader opens the stock-sync README
- **THEN** the README links to `docs/LOCAL_TESTING.md` with the rest of the stock-sync documentation

