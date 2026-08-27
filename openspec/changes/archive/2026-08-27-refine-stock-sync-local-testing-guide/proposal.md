## Why

The stock-sync local testing guide exists, but after running a real local webhook test it should be rewritten in a more human, guided style. The guide should help a non-expert tester understand what the tunnel is, why `localhost:3000` is not a visual page, how to recognize success, and how to clean up a test order without accidentally applying stock changes.

## What Changes

- Refine `automations/stock-sync/docs/LOCAL_TESTING.md` into a more human step-by-step guide.
- Add a guided Shopify -> Mercado Libre test path based on the exact flow validated locally: catcher, cloudflared tunnel, Shopify webhook, test order, SQLite confirmation, dry-run, and cleanup.
- Explain the tunnel concept in plain language.
- Add examples of successful catcher output and dry-run output.
- Add a "how to know the test worked" checklist.
- Add instructions for test-only cleanup: do not run `--apply`, cancel/refund the Shopify test order with restock, remove the temporary webhook, and mark or handle local SQLite tasks so they cannot be applied later by accident.
- Keep the document focused on human local testing rather than troubleshooting.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `stock-sync-handoff-documentation`: Make local testing documentation explicitly human-guided and include validated test-only cleanup behavior.

## Impact

- Affected documentation:
  - `automations/stock-sync/docs/LOCAL_TESTING.md`
- Affected specs:
  - `openspec/specs/stock-sync-handoff-documentation/spec.md`
- No code, API, dependency, database schema, or runtime behavior change is expected.
