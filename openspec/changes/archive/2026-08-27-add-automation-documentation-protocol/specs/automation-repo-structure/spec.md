## ADDED Requirements

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
