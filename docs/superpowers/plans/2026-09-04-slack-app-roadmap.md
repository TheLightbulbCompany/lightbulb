# Slack App — Roadmap and Human/Claude Split

**Spec:** `docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md`
**Goal:** one Slack app in the Slack Marketplace; install → container → chatting with Friday, full feature set.

This document is the execution map across phases. Each phase names what Claude executes (with a plan file), what only a human can do (accounts, consoles, approvals, money), and the gate that lets the next phase start. Phase plans are TDD, subagent-executable, and self-contained; this file is the one place the human checklist lives.

| phase | what ships | Claude executes | human must do | gate to next |
|---|---|---|---|---|
| **0 — Alerting** | page-tier alarms can reach a human | nothing (no code) | re-subscribe `AlertsPage` SNS in prod; confirm PagerDuty/email subscription is `Confirmed` | at least one confirmed subscriber on the prod page topic |
| **1 — Transport** (existing paid owners) | Add-to-Slack button, Events API ingress, bot tokens in Secrets Manager, uninstall handling | `plans/2026-09-04-slack-app-phase1-transport.md` (15 tasks) — **in progress** | create the dev Slack app from the manifest; seed 3 secrets; set Request/Redirect URLs; run the dev walk (Task 15) | dev walk passes; merged; promoted to prod; prod Slack app created + seeded the same way |
| **2 — Cold front door** (free install, no account) | Marketplace/unlisted "Add to Slack" with no Lightbulb account → Clerk org → free container → Friday DMs you | `plans/2026-09-04-slack-app-phase2-cold-install.md` (8 tasks) | add `users:read.email` to the Slack app's scopes (the manifest is the source; re-install once on dev); one throwaway Slack workspace + email for the cold-install walk | cold walk passes on dev; promoted; ≥10 active workspaces + ≥10 WAU accumulating on the unlisted app |
| **3 — Marketplace submission** | listing live in the Slack Marketplace | `plans/2026-09-04-slack-app-phase3-marketplace.md` — drafts every artifact (scope justifications, privacy delta, security answers, screenshots, listing copy) and runs the pre-flight | file the submission; answer reviewer emails within 2 business days; supply test accounts; sign the agreements; hold the 10-workspace floor throughout review (≤10 business days + ≤10 weeks) | listing approved |
| **4 — Depth** | Grid, modals, agent faces, peer-DM privacy, Lambda+SQS ack, Spot skip, warm pool, "which team?" picker | each item gets its own plan when picked | pick and order | — |

## What only a human can do (consolidated)

Exact steps, in the order they become necessary. Everything else in this program is Claude-executable.

### Before Phase 1's dev walk (Task 15)

1. **Create the dev Slack app.** api.slack.com/apps → *Create New App* → *From a manifest* → workspace: the Lightbulb dev workspace → paste `apps/backend/scripts/slack-app-manifest.yaml` (it already carries the `api-dev.flickmeon.ai` URLs) → *Create*.
2. **Install it once** into the dev workspace (*Install to Workspace*). This is the operator's own workspace install; it activates the app so the Request URL can be verified.
3. **Copy three values** from *Basic Information*: Client ID, Client Secret, Signing Secret.
4. **Seed them:**
   ```bash
   aws sso login --profile isol8-admin
   cd apps/backend && uv run python scripts/set_platform_oauth_app.py --env dev --name slack --client-id <CLIENT_ID> --with-signing-secret
   ```
   (prompts for the client secret, then the signing secret).
5. **Verify the Request URL.** *Event Subscriptions* → paste `https://api-dev.flickmeon.ai/api/v1/slack/events` → Slack POSTs `url_verification`; the page turns green when Task 8's route answers. If red: the dev backend with Task 8 isn't deployed yet, or the secret isn't seeded (route returns 503). Same URL under *Interactivity & Shortcuts* and on the `/lightbulb` slash command (the manifest set these, confirm they stuck).
6. **Redirect URL.** *OAuth & Permissions* → `https://api-dev.flickmeon.ai/api/v1/slack/oauth/callback` (manifest set it; confirm).
7. **Activate public distribution** (*Manage Distribution* → *Activate Public Distribution*). Unlisted. This is what makes "Add to Slack" work for workspaces other than the dev one.
8. **Run the dev walk** (Phase 1 Task 15) with Claude-in-Chrome on a PAID dev org admin account.

### Before Phase 1 goes to prod

9. Repeat 1–7 for **prod**: a second Slack app (name "Lightbulb"), manifest with the `api.flickmeon.ai` URLs, seeded with `--env prod`. The dev and prod apps are two distinct Slack apps; only the prod one is ever submitted to the Marketplace.
10. Promote: `gh workflow run deploy.yml --ref main` after the PR merges (prod does NOT auto-deploy).

### Before Phase 2's dev walk

11. Add `users:read.email` to the dev app (the Phase 2 plan updates the manifest; re-apply it in *App Manifest* on api.slack.com, then re-install to the dev workspace so the new scope is granted).
12. Have a **throwaway Slack workspace** whose admin email is NOT a Clerk user (a fresh `+clerk_test`-style address works since the dev Clerk instance is shared with prod previews — pick one nobody will reuse).

### Phase 3 (the parts Slack will only accept from a human)

13. File the submission at api.slack.com/apps → *Submit to Slack Marketplace*. Claude will have drafted every field; you paste, attach, and sign.
14. Agree to: App Developer Policy, Slack Marketplace Agreement, Security Review terms, API ToS, Brand Guidelines.
15. Provide a **security contact** (email + phone) and a **support** address that answers within 2 business days — `support@thelightbulb.company` qualifies if someone owns it.
16. Provide **test accounts** for the reviewer (one paid org admin, one workspace) with credentials, in a workspace pre-populated with sample data.
17. Answer reviewer feedback rounds. Preliminary review ≤10 business days, functional review ≤10 weeks.
18. Keep ≥10 active workspaces and ≥10 weekly active users on the app **throughout** review — the eligibility floor is re-checked.

### Product decisions still owed (any time before Phase 3)

- **Peer-DM disclosure copy** — one sentence on the install page and App Home. Claude drafts; you approve the wording.
- **Marketplace listing copy** — name, tagline, description, category (AI Apps & Assistants). Claude drafts; you approve.
- **Privacy policy delta** — what Slack data is collected/used/retained/deleted + a no-training attestation. Claude drafts against the existing `/legal` pages; counsel/you approve.

## Phase 4 backlog (each becomes a plan when picked)

- **Lambda + SQS ack** — designed in the scale brief; trigger: sustained >50 events/s or Slack's failure email.
- **Per-owner consecutive-503 degrade** — the no-IP 503 is the one deliberate per-owner 5xx (Slack's retry is the recovery); if one dominant workspace stays down for ~an hour it could approach the app-wide 95 %/60 min kill switch. Count consecutive no-IP per owner and degrade to 200 + metric after N.
- **Enterprise Grid** — org-wide install; needs `org_deploy_enabled`, `enterprise_id` routing, and a plugin mode that forbids bindings (so per-agent routing changes shape).
- **Modals** — a small companion plugin; the seam exists (`trigger_id` is handed to plugin interactive handlers).
- **Agent faces** — `chat:write.customize`; Atlas replies as Atlas. One scope + scope justification. (A prod-debugging session reports the owner wants this now — needs the owner's direct say-so, then it is a one-line change in `BOT_SCOPES` + the manifest.)
- **Channel picker at onboarding** (`channels:join`) — Viktor's pattern; the ONLY justified home for that scope, since the plugin never calls `conversations.join` on its own. `not_in_channel` on a post is the bot not being a member — `/invite` fixes it, `channels:join` would not.
- **Peer-DM privacy** — hide peer sessions from the admin session list.
- **"Which team?" picker** — for installers who admin >1 org without a Slack install; Phase 2 rules "most recent org without an install, else create" and names the ceiling.
- **Spot skip on first provision** — only if cold-install latency is observed to suffer from placement fallback.
- **Warm pool** — pre-provisioned unclaimed containers; large (per-owner EFS AP / AIP / gate token are all minted per owner).
- **Slack Connect handling** — Viktor's pattern: an @mention in a shared channel moves to a private DM with an approval before any public post.
- **DM-about-a-thread routing** — replies land back in the thread the DM referenced.

## Working agreements for this program

- Every phase plan is executed subagent-driven with per-task review; final whole-branch review on the most capable model.
- Nothing pushes or merges without a human saying so. Prod promotion is a human dispatch.
- Plans under `docs/superpowers/plans/` are deleted when their phase ships (CLAUDE.md rule); the spec stays until Phase 3 lands, then goes too. This roadmap goes with Phase 3.
