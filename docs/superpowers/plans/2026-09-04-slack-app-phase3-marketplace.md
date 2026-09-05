# Slack App Phase 3 — Marketplace Submission

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Most of this phase is artifact production and a human filing; the engineering tasks are small.

**Goal:** the prod Lightbulb Slack app is listed in the Slack Marketplace (AI Apps & Assistants).

**Spec:** `docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md` §1 (constraints) and §4 (manifest). Phases 1 and 2 are live in prod, and the unlisted app has ≥10 active workspaces and ≥10 weekly active users.

## Global Constraints

- Everything Slack reviews is on the **prod** app only. Dev is never submitted.
- Every scope in the prod manifest has a written, feature-linked justification. Restating the scope's definition is a documented rejection.
- Written attestation: Lightbulb does not train models on Slack data. (True: inference is Bedrock/OpenAI/BYO with no-retention; nothing is retained for training.)
- Human review of consequential actions: the approvals rail (exec approvals, `hire_agent` approvals) is the documented control; it must be ON by default for Slack-connected owners.
- Support response within 2 business days; privacy policy covers collection, use, retention, deletion, contact.
- The eligibility floor (≥10 active workspaces, ≥10 WAU) must hold **throughout** review.

## What Claude produces (artifacts, all under `docs/slack-marketplace/`, deleted after listing)

### Task 1: Scope justification table

**File:** `docs/slack-marketplace/scope-justifications.md`
One row per bot scope in `apps/backend/scripts/slack-app-manifest.yaml`, three columns: scope, the exact feature that needs it (with the code path — e.g. `reactions:write` → ack reaction on mentions, `extensions/slack/src/monitor/provider.ts` ackReaction), and what breaks without it. Includes `users:read.email` → "the installer's email at install time to create their Lightbulb account; called once, at `routers/slack_app.py::_installer_email`". Test: a pytest that every scope in the manifest appears in the table and vice versa (`tests/unit/test_slack_scope_justifications.py`).

### Task 2: Privacy policy delta

**File:** `docs/slack-marketplace/privacy-delta.md` + a PR to the legal pages under `apps/frontend/src/components/legal/` adding a "Slack" section: what we receive (messages in channels the bot is in, DMs to the bot, reactions, file metadata), what we do (forward to the owner's isolated container; the container's session transcripts live on the owner's EFS/S3), retention (until the owner deletes the session or the account is wiped — `account_wipe.py`), deletion (uninstall removes the token immediately; account wipe removes everything), contact (`support@thelightbulb.company`), and the no-training attestation. Founder approves wording before merge.

### Task 3: Security questionnaire answers

**File:** `docs/slack-marketplace/security-answers.md` — drafted answers for Slack's Security & Compliance section: request signing (`core/services/slack_signing.py`), TLS ≥1.2 (API Gateway), token storage (Secrets Manager, never on EFS — `integration_secret_store`), per-tenant isolation (one container per owner, hook_token re-signing), data deletion paths, incident contact. Plus the architecture diagram (a single SVG in the same directory, drawn from spec §3.1).

### Task 4: Listing copy + screenshots

**File:** `docs/slack-marketplace/listing.md` — app name "Lightbulb", tagline, long description, category, support URL, privacy URL, and 5 screenshots captured with Claude-in-Chrome on a prod-like dev workspace: DM with streamed reply and task card, @mention in a channel, an approval card, App Home, the `/lightbulb` slash command. Founder approves copy.

### Task 5: Pre-flight against Slack's guidelines

**File:** `docs/slack-marketplace/preflight.md` — a checklist run against the prod app: no `@channel`/`@everyone` posts, never posts to #general unbidden, no Slackbot-channel notifications, no bulk export/delete, no user impersonation (the bot posts as itself; `chat:write.customize` is NOT requested in this submission), interactivity URL set, shortcuts declared, Socket Mode off, org deploy off, token rotation off, App Home renders, `url_verification` answers, uninstall revokes. Each line has a command or a screenshot reference.

### Task 6: Test workspace + accounts (Claude prepares, human hands over)

- A dedicated prod org for the reviewer with a paid subscription (human pays), seeded with sample Paperclip issues and a couple of agents; a Slack workspace with the prod app installed and three channels with realistic traffic.
- Credentials handed to the human for the submission form, never written to the repo.

### Task 7: Engineering nits the pre-flight surfaces

Small code tasks that fall out of Task 5 — expected candidates: App Home tab content (today a three-block placeholder from upstream; add credits left, connected workspace, a link to the dashboard), a friendlier `/lightbulb` help text, and the `x-slack-no-retry` header on every deliberate drop. Each is a normal TDD task added here when found.

## What only a human does

See the roadmap's "Phase 3" checklist: file the submission, sign the five agreements, provide security + support contacts, hand over test accounts, answer reviewer rounds within 2 business days, keep the floor throughout review.
