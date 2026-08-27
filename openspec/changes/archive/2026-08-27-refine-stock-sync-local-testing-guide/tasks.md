## 1. Human-Guided Local Testing Guide

- [x] 1.1 Rewrite the opening of `automations/stock-sync/docs/LOCAL_TESTING.md` to explain the tunnel and `localhost:3000` in plain human language.
- [x] 1.2 Add a guided Shopify -> Mercado Libre walkthrough based on the validated test flow.
- [x] 1.3 Include the exact kinds of evidence a human should expect in catcher logs, SQLite, and dry-run output.
- [x] 1.4 Add a section explaining that webhook/dry-run proof is enough when the test is only to confirm local behavior.
- [x] 1.5 Add test-only cleanup steps: cancel/refund Shopify order, restock item, remove temporary webhook, stop tunnel/catcher, and neutralize local `ready_to_apply` tasks.
- [x] 1.6 Add a success checklist for the human tester.
- [x] 1.7 Review wording to confirm the guide remains local testing documentation, not troubleshooting.

## 2. Validation

- [x] 2.1 Validate the OpenSpec change.
- [x] 2.2 Review the final diff for documentation clarity and accidental unrelated edits.
