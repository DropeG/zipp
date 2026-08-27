## Why

Future Zipp automations need to be documented with the same structure from the start, so humans and AI agents can understand how to test, operate, hand off, and extend each automation without rediscovering the rules. The current stock synchronization automation also needs a dedicated local testing manual that explains how to imitate production from a developer machine using real webhook delivery.

## What Changes

- Add a repository-wide documentation protocol for every automation under `automations/<automation-id>/docs/`.
- Require each automation to document local production-like testing in `docs/LOCAL_TESTING.md`.
- Define the expected purpose of standard automation documents such as `HANDOFF.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `LOCAL_TESTING.md`, `TROUBLESHOOTING.md`, and `PENDING.md`.
- Add a `LOCAL_TESTING.md` document to the stock synchronization automation that explains how a human can test webhooks locally, including a temporary public tunnel such as `cloudflared`.
- Keep local testing documentation focused on how to test the automation locally, not on diagnosing failures.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `automation-repo-structure`: Add a documentation protocol for future automations, including the standard expectation that automation-specific docs live under `automations/<automation-id>/docs/`.
- `stock-sync-handoff-documentation`: Add local production-like testing documentation for the stock synchronization automation.

## Impact

- Affected documentation:
  - `README.md`
  - `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md`
  - `automations/stock-sync/README.md`
  - `automations/stock-sync/docs/LOCAL_TESTING.md`
- Affected specs:
  - `openspec/specs/automation-repo-structure/spec.md`
  - `openspec/specs/stock-sync-handoff-documentation/spec.md`
- No runtime behavior, API contract, dependency, or database change is expected.
