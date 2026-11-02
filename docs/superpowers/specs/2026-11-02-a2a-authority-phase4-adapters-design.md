# A2A authority extension, phase 4: adapters and publishing

**Status:** draft for the founder's approval. Nothing is built.
**Builds on:** phase 1 (PR #4562) for the door, rules, holds and records; phase 1b (PR #4571) for the in-app answer card; phase 2 is not needed. This branch (`feat/a2a-phase4`) stacks on `feat/a2a-authority-design`.
**Research:** `docs/research/2026-11-02-a2a-phase4-adapters.md` (30 primary pages; every claim below cites it; its "not verified" list applies).
**Scope (the founder's phase list):** Slack, Notion and Copilot Studio shared agents; coding-agent PR review as an adapter; publish the extension page and open the proposal to the A2A project.

## 1. What an adapter is

The hosted door (phase 1) accepts a task only with an authority object minted from a verified identity. Shared agents in other products are triggered by a person the vendor identifies and signs, but none of those products can mint our object. An adapter is a backend component with four duties: (1) receive the vendor's trigger event and verify the vendor's signature; (2) map the vendor's person to a Lightbulb person; (3) mint the authority request to our door with `verified_by` naming the vendor's proof; (4) carry the decision back into the vendor's surface, including a hold that the holder answers there, with the press verified by the vendor's signature and mapped to the holder. Each adapter is a thin translation layer; the rules, holds and records are the phase-1 code, unchanged.

## 2. Goals, non-goals, anti-goals

Goals: four adapters ship behind one `vendor_identities` mapping; a person triggering a shared agent in Slack, Copilot Studio or a GitHub PR gets the same decision, hold and record as a Lightbulb sign-in; the extension page is live at the extension URI; the proposal to the A2A project is drafted and, if the founder says so, opened.
Non-goals: replacing PR review (a second person reviews code; the adapter adds who asked and the record); acting with a vendor user's own token; Notion buttons (the API has none); Slack's user-token MCP; anything that needs phase 2.
Anti-goals: an adapter that mints from an unsigned or unmapped identity; a hold answered by a vendor payload we did not verify; a press whose user is not the holder.

## 3. Identity mapping (shared by every adapter)

`vendor_identities` rows: `{vendor, vendor_user_id, lightbulb_user_id, org, how, verified_at}`. `how` is `oauth` (the person signed in to Lightbulb with that vendor: Sign in with Slack, Microsoft, GitHub) or `admin` (a workspace admin asserted the pairing in settings). Rules can require `oauth` for a resource (`requesters` entries with `how:oauth`, my call: a one-token qualifier, not a `when` clause). A trigger from an unmapped vendor user mints with requester `vendor:<vendor>:<id>` and `verified_by` the vendor's signature; the default rules hold it for the agent's holder, which is the Friday case again.

## 4. The adapters

**GitHub check run (first, my call: it fits the coding-agent fleets that already solved review with a second person and only lack the record).** A GitHub App installed on the repo. When a PR is opened by an agent (our hosted agents, or any PR whose author is a bot or carries an agent trailer), the adapter mints an authority request `skill: pull_request`, `resource: repo:<owner>/<name>`, operands `{pr, head_sha, title}`, requester = the person who asked the agent (from our own task record when the PR came from a Lightbulb task; else the PR author mapped through `vendor_identities`, `verified_by: github-webhook`). It creates a check run with `external_id = task id`, `output.text` = the authority summary (who asked, for whom, intent, record link), and up to three `actions` (`Approve`, `Decline`). The repo's ruleset requires this check from our app, so merge waits on the decision. `check_run.requested_action` (webhook-signed) maps `requested_action.identifier` and the acting user to `a2a_tasks.decide`; `allow` sets the conclusion `success`, `deny` sets `failure`. A requester pressing Approve on their own request gets `self` and the check stays pending, which is GitHub's own rule for Copilot PRs made explicit.

**Slack.** A Slack app with event subscriptions (`app_mention`, `message.im`, `assistant_thread_started`) and the request signature check (`X-Slack-Signature`, 5-minute skew). The triggering person is `event.user` (or `assistant_thread.user_id`); the adapter mints `skill: message`, `resource: agent:<id>`, operands `{text}`, `verified_by: slack-signature`, and calls the door. A hold posts a Block Kit message in the holder's DM with `Approve` / `Decline` buttons; the `block_actions` payload's `user.id` must map to the holder; the ack goes within 3 s and the decision follows through `response_url` (5 uses in 30 minutes). The bot speaks as the bot, never with the person's token.

**Copilot Studio front door.** Copilot Studio can add our door as an A2A agent (endpoint URL, auth None, API key or OAuth 2.0), but it sends the 0.3 method name `message/send` and no user id, only a chat-history metadata block. The adapter is a second endpoint `/a2a-cs/{org}/{agent}` that accepts the 0.3 envelope, authenticates with the API key or OAuth the maker configured, takes the person from the per-user authentication claims when the agent uses "User authentication" (`User.Id`, mapped through `vendor_identities`, `verified_by: copilot-user-auth`) or marks the request `verified_by: copilot-maker` when the agent runs on maker credentials, and mints accordingly. Default rules hold maker-credential requests (the autonomous-run case the vendor itself warns about). Holds are answered in Lightbulb (phase 1b's card) because adaptive-card callbacks are unverified.

**Notion (notify-only).** Notion webhooks carry `authors[]` with `id` and `type` (person, bot, agent); there is no button block. The adapter mints from a webhook author of type `person`, and on a hold posts a comment (`POST /v1/comments`) with the request summary and a link; the holder answers in Lightbulb. Ships last, my call, because it can only notify.

## 5. Publishing and the proposal

- The extension page (`docs/a2a-authority-extension.md`) is served at the extension URI `https://thelightbulb.company/a2a/ext/authority/v1` and linked from the Agent Card's extension `description`. The builder finds the site's route convention (the monorepo frontend) and adds the page; the URI then resolves to its own spec, which is what the A2A extensions guide expects.
- The proposal: an issue in `a2aproject/A2A` with an abstract, a motivation (A2A leaves authorization to the implementation and carries no delegation chain; the extension binds subject chain, operation and operands and records the decision) and the technical approach (this page), naming the reference implementation (our door) and Apache 2.0 for the spec text. A maintainer must sponsor it into an `experimental-ext-authority` repo; graduation needs a TSC vote. The founder said: publish at launch, propose later if we want. The draft issue text is written in this phase and opened only on his word.

## 6. Tests (acceptance)

1. GitHub: a signed `check_run.requested_action` from the holder allows the task and the check concludes `success`; from the requester it is refused `self` and the check stays pending; an unsigned webhook is rejected.
2. Slack: a signed `app_mention` from a mapped user mints and is allowed by rule; from an unmapped user it is held; the holder's `block_actions` press allows it; a press by another user is ignored; a stale signature is rejected.
3. Copilot Studio: a `message/send` envelope with per-user auth claims mints with `verified_by: copilot-user-auth`; one on maker credentials is held by default.
4. Notion: a `comment.created` by a person mints; a hold posts one comment with the link.
5. The extension URI returns the page with HTTP 200 and the Agent Card links to it.
6. Phase-1 suites pass unchanged.

## 7. Left to the builder

The GitHub App manifest and the exact permissions (`checks:write`, `pull_requests:write`, the webhook events); the Slack app manifest and scopes; how the Copilot Studio API key is provisioned per workspace; the site route for the extension page; the vendor-identity settings UI (admin assertion); rate limits and retries per vendor.

## 8. Questions for the founder

1. Order: GitHub check run, then Slack, then Copilot Studio, then Notion. Confirm, or reorder.
2. Identity mapping: is an admin's assertion enough for v1, or must each person sign in to Lightbulb with the vendor before their triggers count as theirs?
3. The A2A proposal: open the issue when the page is live, or wait for a design partner using the extension?
