# automation-repo-structure Specification

## Purpose
TBD - created by archiving change organize-automation-repo-structure. Update Purpose after archive.
## Requirements
### Requirement: Repository identifies itself as a general automation hub
The repository SHALL present itself as the home for Zipp automations rather than as a repository dedicated only to the current stock synchronizer.

#### Scenario: Reader opens root README
- **WHEN** a reader opens the root `README.md`
- **THEN** the document describes the repository's general purpose and links to available automations

#### Scenario: Future automation is added
- **WHEN** a new automation is added to the repository
- **THEN** it has a clear place under `automations/` without mixing its primary files into the repository root

### Requirement: Automations use a consistent folder contract
Each concrete automation SHALL live under `automations/<automation-id>/` and use a consistent internal structure for source files, tests, and documentation.

#### Scenario: Stock sync automation exists
- **WHEN** the stock synchronization automation is organized
- **THEN** its canonical folder is `automations/stock-sync/`

#### Scenario: Automation has local docs
- **WHEN** an automation requires handoff or operational documentation
- **THEN** that documentation lives inside the automation folder rather than only in repository-level docs

### Requirement: Root stays lightweight
The repository root SHALL contain only global project files, repository-wide documentation, dependency manifests, ignored runtime directories, and links into automation-specific folders.

#### Scenario: User scans root files
- **WHEN** a user scans the repository root
- **THEN** they can distinguish global files from automation-specific implementation files

#### Scenario: Documentation belongs to one automation
- **WHEN** documentation describes only one automation
- **THEN** it is stored under that automation's folder

### Requirement: Shared integrations are separated from workflows
Reusable integration clients and helpers SHALL be separated from automation-specific workflows when they are likely to be reused by multiple automations.

#### Scenario: Shopify helper is reused
- **WHEN** another automation needs Shopify API access
- **THEN** it can depend on a shared Shopify helper instead of copying stock-sync workflow code

#### Scenario: Workflow-specific processor exists
- **WHEN** a script implements the stock-sync workflow
- **THEN** it remains under the stock-sync automation rather than the shared helpers area

### Requirement: Runtime state remains outside version control
The repository SHALL exclude secrets, tokens, local databases, logs, generated reports, and temporary assets from version control.

#### Scenario: Developer checks Git status
- **WHEN** local runtime files such as `.env`, `meli_tokens.json`, `data/stock_sync.db`, or logs exist
- **THEN** they are ignored and do not appear as files to commit

#### Scenario: Server operator clones repository
- **WHEN** the repository is cloned onto a server
- **THEN** secrets and runtime state are created locally from documented setup steps, not pulled from Git

### Requirement: Automations follow a documentation protocol
The repository SHALL define a documentation protocol that future automations can follow to keep setup, local testing, operation, troubleshooting, handoff, and pending work consistently documented.

#### Scenario: Future automation is documented
- **WHEN** a future automation is added under `automations/<automation-id>/`
- **THEN** the repository documentation explains which automation-local documents should be created and what each document is responsible for

#### Scenario: AI agent documents an automation
- **WHEN** an AI agent is asked to document a new automation
- **THEN** it can follow a repository-level protocol instead of inventing a new documentation structure

### Requirement: Local testing documentation is a standard automation document
Automation documentation SHALL treat `docs/LOCAL_TESTING.md` as the canonical place for human instructions on testing an automation locally while imitating production.

#### Scenario: Human tests an automation locally
- **WHEN** a human wants to test an automation from a developer machine before server deployment
- **THEN** the automation documentation points them to `automations/<automation-id>/docs/LOCAL_TESTING.md`

#### Scenario: Local testing differs from operations
- **WHEN** an automation has both daily operation steps and production-like local test steps
- **THEN** daily operation remains in `docs/OPERATIONS.md` and local production-like testing remains in `docs/LOCAL_TESTING.md`

