## Context

Zipp is now organized as a general automation repository. The stock synchronization automation already has local documentation for handoff, architecture, operations, troubleshooting, pending work, and historical context, but there is no repository-level protocol that tells future humans or AI agents which documents every new automation should include.

The user wants future automations to stay ordered from the beginning. In particular, each automation should have a human-facing local testing guide that explains how to run the automation from a developer machine while imitating production as closely as possible.

## Goals / Non-Goals

**Goals:**

- Establish a reusable documentation protocol for every folder under `automations/<automation-id>/`.
- Add `LOCAL_TESTING.md` as a standard automation document.
- Document what each standard automation document is responsible for.
- Add stock-sync local testing documentation as the first concrete example of the protocol.
- Keep the protocol useful for humans and AI agents.

**Non-Goals:**

- Change synchronizer runtime behavior.
- Add new webhook security, deployment automation, or cron/systemd behavior.
- Replace existing stock-sync handoff, operations, architecture, troubleshooting, pending, or historical documents.
- Document inactive future automations as public/active work before the user wants them exposed.

## Decisions

### Use A Repository-Level Protocol Document

Create `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md` as the canonical rulebook for future automations.

Alternative considered: describe the protocol only in the root README. That would make the README too noisy and harder to keep focused on active automations.

### Keep Documentation Inside Each Automation

Each automation keeps its own detailed docs under `automations/<automation-id>/docs/`.

Alternative considered: store all docs under repository-level `docs/`. That would separate documentation from the automation it explains and make future growth harder to scan.

### Standardize `LOCAL_TESTING.md`

Every automation that can be tested locally gets a `docs/LOCAL_TESTING.md` file focused on how a human can run local tests that imitate production.

For stock-sync, this means documenting:

- local environment checks
- starting the webhook catcher
- exposing `localhost:3000` through a temporary public tunnel such as `cloudflared`
- configuring temporary webhook URLs
- generating a test event
- checking SQLite tables
- running dry-run
- applying one controlled change only after dry-run is understood
- cleaning up temporary webhook/tunnel setup

Alternative considered: add these steps to `OPERATIONS.md`. That mixes everyday operation with local production-like testing and makes both documents less clear.

### Keep `LOCAL_TESTING.md` Separate From Troubleshooting

`LOCAL_TESTING.md` describes the happy-path/manual test flow. Troubleshooting remains focused on diagnosis and recovery after something does not work.

Alternative considered: merge local testing and troubleshooting. The user explicitly clarified that local testing is for humans to test the automation, not for explaining problems.

## Risks / Trade-offs

- Documentation protocol becomes too rigid → Keep required documents purpose-based, and allow automations to omit files that truly do not apply.
- README becomes noisy again → Keep the full protocol in `docs/AUTOMATION_DOCUMENTATION_PROTOCOL.md` and link to it from the README or automation docs.
- Local tests accidentally touch real stock → Stock-sync local testing must emphasize dry-run first, `--apply --limit 1` only when intentionally testing a real stock update, and stopping automatic processors during manual tests.
- Future AI agents over-document empty automations → Protocol should say to document active/real automations and avoid presenting parking folders as completed automations.

## Migration Plan

1. Add the repository-level documentation protocol.
2. Add `automations/stock-sync/docs/LOCAL_TESTING.md`.
3. Update `automations/stock-sync/README.md` to link to the new local testing guide.
4. Update the root README only if needed to point future contributors/AI agents to the documentation protocol.
5. Validate OpenSpec and review generated docs for consistency.
