# Credentials and granted secrets

## Proposing Credentials Safely

**Never paste a credential into an issue comment, document, file, plan, task
description, transcript, or work product.** This applies whether the value
was pasted by a user, returned by an OAuth flow, delivered by email, or
obtained from another secure source.

`POST /api/agents/me/secret-proposals` — upstream's "propose it as a secret"
call — **403s on this instance for every agent** with `"Secret proposals
require a verified run-bound agent token"`. It requires a run-bound agent
JWT, and every Lightbulb agent runs the `hermes_gateway` adapter, which
never mints one — you only ever hold a long-lived agent key. Do not call it.

Instead: tell the owner in a comment that a credential is needed and what
it's for, and let them add it through the Lightbulb UI. Once granted, read it
back with the calls in "Reading Granted Secrets" below — those work with the
long-lived agent key you actually have.

## Reading Granted Secrets

On a heartbeat run, list the secrets available to that run before fetching a
value:

`/app/skills/paperclip/scripts/paperclip-api.sh raw GET /api/agents/me/secrets`

The list is metadata-only. Fetch a specific value only when needed (no body):

`/app/skills/paperclip/scripts/paperclip-api.sh raw POST /api/agents/me/secrets/<name>/value`

- An `env.*` secret binding also grants API read access; `access.*` bindings grant API access without env injection.
- Prefer env injection for values needed on every run; prefer on-demand fetch for values used only on some runs, large/structured values, or skills/tools that do not inherit adapter env.
- Every value fetch, including failures, is audited (`secret_access_events`, `activity_log`) — never print, persist, or paste fetched values into task comments.
- These endpoints check only that you're an authenticated agent with a `companyId` and a run id — they do **not** check token type, so the long-lived agent key every Lightbulb agent holds works fine here. The real constraint is the run id: these calls work on a heartbeat run (`paperclip-api.sh` always sends `X-Paperclip-Run-Id` there) and fail in a webchat session, which has none — same rule as every other run-scoped call in this skill (see "Run audit trail" in SKILL.md).
