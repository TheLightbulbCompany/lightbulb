# Friday Orchestration + Marketplace Seller Usernames — Rollout Checklist

**For:** dev reviewing the 5 PRs landed on 2026-05-13.
**Goal:** one place that captures everything beyond the commits themselves — PR order, the open decision, operator actions, cleanup.

---

## The 5 PRs

| # | Branch | What | Open at |
|---|---|---|---|
| 1 | `feat/friday-playbook-and-greeting` | Friday gets `AGENTS.md` (orchestration playbook + in-house roster + triage rules) and a one-line BOOTSTRAP greeting tweak that telegraphs orchestration | [pull/new/feat/friday-playbook-and-greeting](https://github.com/Isol8AI/isol8/pull/new/feat/friday-playbook-and-greeting) |
| 2 | `feat/friday-orchestration-tools-service` | New `friday_tools.py` with `paperclip_list_agents` / `paperclip_create_issue` / `paperclip_get_issue_status` (the service layer Friday will call) | [pull/new/feat/friday-orchestration-tools-service](https://github.com/Isol8AI/isol8/pull/new/feat/friday-orchestration-tools-service) |
| 3 | `feat/friday-polling-pass-scaffold` | New `friday_poll.py` registered in `update_service.run_scheduled_worker`. Detects issue status changes and wakes Friday via `chat.send` RPC | [pull/new/feat/friday-polling-pass-scaffold](https://github.com/Isol8AI/isol8/pull/new/feat/friday-polling-pass-scaffold) |
| 4 | `feat/marketplace-seller-usernames` | Seller usernames claimed at first publish. New DDB table, backend service + router, publish gate, marketplace card display, settings panel | [pull/new/feat/marketplace-seller-usernames](https://github.com/Isol8AI/isol8/pull/new/feat/marketplace-seller-usernames) |
| 5 | `feat/seed-atlas-at-provision-org` | Seed Atlas (role=recruiter) alongside Friday in `provision_org` so the "hire a new agent" delegation has a real target on day one | [pull/new/feat/seed-atlas-at-provision-org](https://github.com/Isol8AI/isol8/pull/new/feat/seed-atlas-at-provision-org) |

**Dependencies between PRs:** none — each was branched from `main` (HEAD `bb39360f`) and touches different files. They can be reviewed and merged in any order. Suggested review order is the PR number above (1 → 5) since that matches the conceptual layering (identity → tools → wake → marketplace → atlas), but it's not enforced.

---

## The one open decision blocking the orchestration loop end-to-end

**PR 2's HTTP boundary is not wired.** The service functions (`friday_tools.py`) work and are tested. What's missing is the transport that lets Friday's OpenClaw prompt actually *invoke* them from inside the container.

Two options, your call:

### Option A — Generic HTTP fetch tool + REST endpoints

- Backend exposes three REST endpoints (e.g. `POST /api/v1/hooks/friday/create-issue`, `GET /api/v1/hooks/friday/agents`, etc.)
- Auth via the container's `hook_token` as bearer (same token shipped in [#593](https://github.com/Isol8AI/isol8/pull/593))
- Friday's `openclaw.json` registers an HTTP fetch tool with the backend URL allowlisted
- Backend handler reads `owner_id` from the authenticated `hook_token` (load-bearing: NEVER from the request body — that's the cross-tenant escape) and dispatches to the `friday_tools` functions

**Pros:** Reuses the hook-token plumbing that already exists. Cleanest auth model. Smallest code surface.
**Cons:** Friday uses a generic fetch tool, so her prompt has to describe the call shape (URL, body, what success/error looks like) rather than calling a typed tool.

### Option B — Custom OpenClaw plugin

- Write a Paperclip-aware OpenClaw plugin that wraps the three calls as native tools
- Plugin handles auth internally; exposes typed `paperclip.list_agents()` / `paperclip.create_issue(...)` to the agent
- Plugin distributed via the existing plugin pipeline; registered in Friday's `openclaw.json` `plugins.entries`

**Pros:** Friday's prompt sees real tools, not a generic fetch. Cleaner agent ergonomics. Better long-term shape if more tools land later.
**Cons:** More code. Requires touching OpenClaw plugin scaffolding. Slower to land.

**Recommendation:** Option A for v1 since the hook-token plumbing already exists and the marginal LLM tokens to teach Friday the call shape are cheap. Graduate to Option B if more orchestration tools accumulate.

Once you pick, this is a small follow-up PR (≤ 200 lines of code + tests).

---

## Other open follow-ups (smaller, mostly out of band)

### PR 2 sibling — the wake message Friday receives on triage

PR 3's wake sends Friday a plaintext `chat.send` message describing the status change. Right now it says:

> "Issue {id} just changed status: {prev} → {new}. Call paperclip_get_issue_status to read the latest, then triage…"

That references `paperclip_get_issue_status` — which only works once PR 2's HTTP boundary lands. Until then, Friday will read the message, recognize the tool name from her `AGENTS.md`, attempt to call it, fail because it doesn't exist, and report the failure to the user in the next interactive `/chat`. Acceptable as a soft failure; not breaking, just not yet useful.

### Issue creator filter

`friday_poll._friday_poll_pass` reads ALL open issues per company, not just "issues Friday created". Adding the targeted filter needs either:
- A `creator_agent_id` filter on Paperclip's `list_issues` (not currently exposed), or
- An Isol8-side mapping table populated when Friday calls `paperclip_create_issue`

Filed as a comment in `friday_poll.py`. Defer until the table grows.

---

## Operator actions after merging

These can't be automated by the PRs themselves — they're deploy-time or runtime.

### After PR 4 (seller usernames) merges

- [ ] **Deploy CDK** — the new `seller-usernames` DDB table needs to land before the backend reads it. Standard `deploy.yml` flow; the change is additive (new table, IAM grant, env var) so no migration.

- [ ] **Seed `@isol8` username** — one row in `seller-usernames` for `username_lower="isol8"` pointing at the admin Clerk `user_id`, and the mirror columns on the admin user's row in the `users` table. Without this row, in-house listings render via `resolve_seller_display`'s safety fallback (which displays "@isol8" anyway, but it's implicit — explicit row is the correct shape).

  Documented inline in `apps/infra/lib/stacks/database-stack.ts` next to the table definition.

### After PR 1 (AGENTS.md) and PR 5 (Atlas seeding) merge

- [ ] **Reprovision existing dev/test containers.** Both PR 1 and PR 5 use write-once semantics — they only fire on first provision. Existing containers won't pick up the new `AGENTS.md` or get Atlas seeded until reprovisioned.

  Per `CLAUDE.md` "Container Re-provisioning":

  ```javascript
  // Browser console on any Isol8 page (e.g. dev.isol8.co/chat):
  // PATCH — silent rewrite of openclaw.json + agent files; preserves the container
  await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {
    method: 'PATCH',
    headers: { Authorization: 'Bearer ' + await Clerk.session.getToken() }
  }).then(r => r.json())

  // OR full reprovision (DELETE then POST) — slower but exercises the full path
  await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {
    method: 'DELETE',
    headers: { Authorization: 'Bearer ' + await Clerk.session.getToken() }
  }).then(r => r.json())
  await fetch('https://api-dev.isol8.co/api/v1/debug/provision', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + await Clerk.session.getToken() }
  }).then(r => r.json())
  ```

  New signups get all of this automatically — only existing containers need the manual touch.

### After PR 3 (polling + wake) merges

- [ ] **Confirm the scheduled worker is healthy.** Look at CloudWatch for the `friday_poll.pass.heartbeat` metric. Should fire at most once per `_POLL_INTERVAL_SECONDS` (5 minutes by default). If not, the worker is throttled or crashed silently — check logs for `friday_poll: unexpected error in pass`.

- [ ] **Tail the `friday_poll.wake.success` and `.error` / `.container_unreachable` metrics** for the first hour to verify wakes are actually firing as expected against your test container. If `.container_unreachable` is dominating, that's just the scale-to-zero reaper kicking in — fine. If `.error` is dominating, something's wrong with `send_rpc` for the poll session.

---

## Verification — does the orchestration loop work end-to-end?

After all 5 PRs merge, CDK deploys, you seed `@isol8`, and you reprovision your dev container, you should be able to:

1. Open `/chat` and see Friday + Atlas + any other agents you've installed in the sidebar
2. Ask Friday: "What agents do I have?" — should respond from her in-built roster (or call `paperclip_list_agents` once PR 2's HTTP boundary lands)
3. Ask Friday: "I need an agent who handles competitive intel" — should describe the decompose → recommend Atlas / marketplace flow
4. Publish a marketplace listing — should require a username first; the published card should show `by @yourhandle`

The end-to-end "Friday creates issues, gets woken on status changes, triages and reports back" loop will work only after PR 2's HTTP boundary follow-up lands. Without it, Friday can describe the loop but not actually create issues.

---

## Cleanup (optional, low-priority)

- Two stale subagent worktrees at `.claude/worktrees/agent-a3b5c94e370f1f81f` and `.claude/worktrees/agent-a2d4ca8dd9f350d3e`. Branches are already on the remote; worktrees are just local artifacts. Remove with `git worktree remove <path>` when convenient.

---

## Design docs (background reading, not action items)

- [Friday Orchestration Design](../specs/2026-05-13-friday-orchestration-design.md) — the spec the PRs implement
- [Friday Orchestration Plan](2026-05-13-friday-orchestration-plan.md) — the TDD-structured implementation plan
- [Paperclip ↔ OpenClaw Pairing](../../architecture/2026-05-07-paperclip-openclaw-pairing.md) — agent identity and the issue-driven session model
- [Friday Identity Seeding](../specs/2026-05-08-friday-identity-seeding-design.md) — the existing PR #573 work this builds on
