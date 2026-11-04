# A2A authority extension, phase 4: adapters and publishing

**Status:** draft for the founder's approval. Nothing is built.
**Builds on:** phase 1 (PR #4562) for the door, rules, holds and records; phase 1b (PR #4571) for the in-app answer card; phase 2 is not needed. This branch (`feat/a2a-phase4`) stacks on `feat/a2a-authority-design`.
**Research:** `docs/research/2026-11-02-a2a-phase4-adapters.md` (30 primary pages; every claim below cites it; its "not verified" list applies).
**Builder check (2 Nov):** run; ten contradictions and eleven questions folded in; calls marked "my call".
**Scope (the founder's phase list):** Slack, Notion and Copilot Studio shared agents; coding-agent PR review as an adapter; publish the extension page and open the proposal to the A2A project.

## 1. What an adapter is

The hosted door (phase 1) accepts a task only from a Lightbulb sign-in, mints the requester itself, and allows only `skill: message` to `agent:<id>` (`a2a_tasks.py:150-174`, `routers/a2a.py:83`). An adapter therefore does not call the door. It uses the in-process entry Friday uses (`ask_session`, `a2a_tasks.py:275-324`): a new `a2a_tasks.vendor_request(org, *, vendor, vendor_user_id, person, verified_by, skill, resource, operands, intent, agent_id, on_allow, on_hold, on_deny)` that mints in-process with the adapter's own `verified_by`, runs `_decide_and_store`, and lets the adapter say what an allow, a hold or a deny looks like in the vendor's surface. `_decide_and_store` gains `on_allow` (the refactor phase 3 also needs) and `on_hold`; the Lightbulb DM notification stays as the holder's second channel. So the phase-1 code changes in one place, the callbacks; the rules, holds, records and decide route are unchanged.

An adapter's four duties: (1) receive the vendor's trigger event and verify the vendor's signature; (2) map the install to an org and the vendor's person to a Lightbulb person; (3) mint through `vendor_request`; (4) carry the decision back into the vendor's surface, including a hold the holder answers there, with the press verified by the vendor's signature and mapped to the holder.

## 2. Goals, non-goals, anti-goals

Goals: four adapters ship behind one `vendor_identities` mapping; a person triggering a shared agent in Slack, Copilot Studio or a GitHub PR gets the same decision, hold and record as a Lightbulb sign-in; the extension page is live at the extension URI; the proposal to the A2A project is drafted and, if the founder says so, opened.
Non-goals: replacing PR review (a second person reviews code; the adapter adds who asked and the record); acting with a vendor user's own token; Notion buttons (the API has none); Slack's user-token MCP; anything that needs phase 2.
Anti-goals: an adapter that mints from an unsigned or unmapped identity; a hold answered by a vendor payload we did not verify; a press whose user is not the holder.

## 3. Identity mapping (shared by every adapter)

Two tables. `vendor_installs`: `{vendor, install_id, org, installed_by, installed_at}` (Slack `team_id` already maps to an owner in `slack_installs_repo`, reused; GitHub installation id; Copilot Studio API key id; Notion workspace id). `vendor_identities`: `{vendor, vendor_user_id, lightbulb_user_id, org, how, verified_at}` where `how` is `oauth` (the person signed in to Lightbulb with that vendor) or `admin` (a workspace admin asserted the pairing in settings; there is no Clerk external-account lookup in the code, so `oauth` rows are written by the vendor sign-in callback the builder adds per vendor).

Rules grammar (builder-check contradiction 2): `requester_matches` (`a2a_rules.py:75-78`) gains `vendor:<vendor>:<id>` (exact) and `vendor:<vendor>:*`; `org:<id>` matches only person requesters (`user_...`), my call, so default rule r1 never allows a vendor requester. A mapped vendor user mints as their Lightbulb person with `verified_by` naming the vendor proof (`slack-signature`, `github-webhook`, `copilot-maker`, `notion-webhook`); an unmapped one mints as `vendor:<vendor>:<id>`. Rules that must insist on a stronger proof use phase 1b's `when` clause with a new field `verified_by_in: [...]` (a one-field addition to 1b's `When`; no `how:oauth` qualifier). Holders when no document is stored: `agent:*` holders are the install's `installed_by` admin, injected at decide time (the pattern phase 3 uses for the wallet), so an unmapped user's request is held for the person who connected the vendor, which is the Friday case.

## 4. The adapters

**GitHub check run (first, my call: coding-agent fleets already solved review with a second person and only lack the record).** A new GitHub App installed on the repo (the backend has `X-Hub-Signature-256` handling to reuse, `routers/meta_webhook.py:223`, but no GitHub App). Agent PRs are those whose head was pushed by a Lightbulb task (our record names the person who asked) or whose author login is on the install's bot list; others are ignored. The adapter calls `vendor_request` with `skill: pull_request`, `resource: repo:<owner>/<name>`, operands `{pr, head_sha, title}`, requester from the task record or the mapped author, `verified_by: github-webhook`. `on_hold` creates a check run with `external_id = task id`, `conclusion: action_required`, `output.text` = the authority summary (who asked, for whom, intent, record link) and `actions` `Approve` / `Decline`; `on_allow` sets `success`; `on_deny` sets `failure`. The repo's ruleset requires this check from our app, so merge waits on the decision. `check_run.requested_action` (webhook-signed) maps the acting user (`sender`, which the builder verifies against GitHub's payload reference) through `vendor_identities` and calls `a2a_tasks.decide`; the requester pressing Approve gets `self` and the check stays `action_required`, GitHub's own rule for Copilot PRs made explicit.

**Slack.** Not a second app: Slack allows one Request URL per app, and `routers/slack_app.py:120` already receives every workspace's events, maps `team_id` to an owner and forwards them to that owner's container, including `block_actions`. The adapter is a branch in that router: after the signature check (`core/services/slack_signing.py`) and dedup (`webhook_dedup.py`), it acks within 3 s and runs the decision in a background task; `allow` forwards the event to the container as today; `hold` posts Block Kit `Approve` / `Decline` buttons in the holder's Slack DM and the Lightbulb DM; `deny` replies in thread with the refusal. The `block_actions` press maps `user.id` to the holder and follows `response_url` (5 uses in 30 minutes). The bot speaks as the bot, never with the person's token.

**Copilot Studio front door.** Copilot Studio can add our door as an A2A agent, but its call carries no user identity and uses the 0.3 method name (`message/send`; both the name and the per-user question are on the research's unverified list). The front door `/a2a-cs/{org}/{agent}` accepts that envelope, authenticates with the per-workspace API key (which also names the install), keeps a `contextId → room` table so turns reuse one room (builder-check contradiction 7; `to_a2a_task` reports the mapped `contextId`), and mints every request as `verified_by: copilot-maker`, which the default rules hold for the install's admin; if a per-user identity turns out to be available on the A2A connection, the builder maps it through `vendor_identities` with `verified_by: copilot-user-auth`. Holds are answered in Lightbulb (phase 1b's card), so this adapter waits for 1b.

**Notion (notify-only, last).** Notion webhooks carry `authors[]` with `id` and `type`; the signature scheme is unverified (the builder reads Notion's docs first). No button block exists, so `on_hold` posts a comment with the summary and a link; the holder answers in Lightbulb. Notion's Slack-trigger route is out of scope.

## 5. Publishing and the proposal

- The extension page (`docs/a2a-authority-extension.md`) is served at the extension URI `https://thelightbulb.company/a2a/ext/authority/v1` and linked from the Agent Card's extension `description`. Two sites exist (`apps/frontend/src/app` with docs and blog routes, and `apps/company`); the builder picks the one serving the apex and renders the page from the repo file at build time rather than copying it, my call, so the URI resolves to the same text the repo holds.
- The proposal: an issue in `a2aproject/A2A` with an abstract, a motivation (A2A leaves authorization to the implementation and carries no delegation chain; the extension binds subject chain, operation and operands and records the decision) and the technical approach (this page), naming the reference implementation (our door) and Apache 2.0 for the spec text. A maintainer must sponsor it into an `experimental-ext-authority` repo; graduation needs a TSC vote. The founder said: publish at launch, propose later if we want. The draft issue text is written in this phase and opened only on his word.

## 6. Tests (acceptance)

1. GitHub: a signed `check_run.requested_action` from the holder allows the task and the check concludes `success`; from the requester it is refused `self` and the check stays pending; an unsigned webhook is rejected.
2. Slack: a signed `app_mention` from a mapped user mints and is allowed by rule; from an unmapped user it is held; the holder's `block_actions` press allows it; a press by another user is ignored; a stale signature is rejected.
3. Copilot Studio: a `message/send` envelope with per-user auth claims mints with `verified_by: copilot-user-auth`; one on maker credentials is held by default.
4. Notion: a `comment.created` by a person mints; a hold posts one comment with the link.
5. The extension URI returns the page with HTTP 200 and the Agent Card links to it.
6. Phase-1 suites pass unchanged.

## 7. Left to the builder

The `vendor_request` signature and the `on_allow`/`on_hold`/`on_deny` callback shapes (shared with phase 3's `on_allow`); the GitHub App manifest and permissions (`checks:write`, `pull_requests:write`, the webhook events) and the `sender` field check; the Slack scopes added to the existing app and the background-task plumbing after the 3 s ack; how the Copilot Studio API key is provisioned per workspace; the Notion signature scheme; the vendor-identity and bot-list settings UI; the site route for the extension page; rate limits and retries per vendor.

## 8. Questions for the founder

1. Order: GitHub check run, then Slack, then Copilot Studio (after 1b), then Notion. Confirm, or reorder. This phase stacks on phase 1 like 1b and 3; the base-branch question is the same one asked for 1b.
2. Identity mapping: is an admin's assertion enough for v1, or must each person sign in to Lightbulb with the vendor before their triggers count as theirs?
3. The A2A proposal: open the issue when the page is live, or wait for a design partner using the extension?
