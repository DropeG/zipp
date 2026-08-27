## ADDED Requirements

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
