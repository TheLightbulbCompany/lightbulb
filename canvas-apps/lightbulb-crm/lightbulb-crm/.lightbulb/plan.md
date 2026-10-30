# Internal tool on browser-local data

Switches: browser-local persistence; Canvas controls room access.

**Builds first:** adapt the existing CRM prototype into the native Canvas app without changing its local-first data model.

**Asks:** none. The request defines the room, workflows, and persistence model.

**Done when:** the Canvas preview renders and the pipeline/contact views, search, filters, follow-ups, CRUD, metrics, JSON import/export, and local persistence work in the actual Canvas UI.

# Plan

## What was asked

Publish the existing functional CRM as a native Lightbulb Canvas app for Aden, not as a chat attachment.

## Builds first

- [x] Reuse the existing CRM HTML and browser-local persistence.
- [x] Render it from the native app page behind the Canvas door.
- [x] Cover the native page and required workflows with focused tests.

## Done when

- [x] `lightbulb app check` and focused tests pass.
- [ ] The preview is built and appears in Canvas.
- [ ] The main workflow is exercised in the actual Canvas UI and recorded as evidence.

## Checks

- `pnpm test -- src/app/page.test.tsx`
- `lightbulb app check`
- `lightbulb app up --json --requested-by "Aden"`
