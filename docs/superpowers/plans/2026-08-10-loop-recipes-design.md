# Loop Recipes — design + first build (support-inbox)

A recipe = a packaged operating loop a non-technical owner can activate.
Design rule applied throughout: the lazy version that works today ships; every
platform feature had to prove it was load-bearing or it got cut (log at the
bottom). All code claims below verified against the repo this session.

---

## Recipe 1 — Support / Inbox loop  ✅ built in this PR

**The headline question — does it need ANY platform code?**
**No. Zero.** The listing is pure content (a one-agent `agentcompanies/v1`
package + seed prose) through the existing `__isol8__` ingest path. Email
needs *nothing provisioned at deploy time*: SES inbound is fleet-wide
(`ses-inbound-stack`), addresses are lazy data rows, per-thread sessions
(`agent_mail_thread.thread_session_key`) and the reply script already work
for any agent. The "listings can't provision triggers" gap **does not bite
this recipe at all.**

But the "agent mints its own address" claim needed the challenge it got, and
it is only **half-true today** — two precise findings:

1. **The lazy mint is Friday-hardcoded.** `agent_mail_owner_send.send_to_owner`
   resolves `friday_agent_id` and mints `agent_name="Friday"` — a hired
   support agent's first send goes out from the *company* address (Friday's),
   never its own. Inbound routing, delivery (`deliver_to_agent`), and the
   reply path are already fully per-agent — only the mint caller is not.
2. **Every address is private.** `mint_agent_email_address` always writes
   `verified_senders=[owner_email]`, `public=False`; nothing in the codebase
   ever sets `public=True` or adds a sender (the repo and the
   `ses_inbound` gate both already support both). So a Gmail **auto**-forward
   rule — which preserves the customer's `From:` — drops at
   `dropped_unverified_sender`. A **manual** forward (From = owner) sails
   through.

**The v0 that ships anyway (and is honestly good):** the owner *manually
forwards* a customer email. It passes the allowlist, wakes the company with a
stable per-thread session, and the reply-token replies **only to the sender —
the owner**. The "never auto-send to third parties" rule is therefore
**structural, not prose**: today the platform is physically incapable of
mailing a customer. Human-in-the-loop is enforced by architecture.

**Owner activation UX, step by step (v0):**
1. Hire "Support Inbox" from the marketplace (existing hire flow).
2. Say hello to Support Triage in the Teams inbox; it walks them to a company
   email address (first-contact mint, existing flow — name the company first).
3. **The ONE thing the owner does themselves: forward a customer email to
   that address.** No dashboard, no webhook, no OAuth.
4. Back comes a triaged draft, by email and in the thread. Owner approves /
   tweaks / sends. Follow-ups on the same thread hit the same session.

**Platform provisions:** nothing new (container, Paperclip agent, workspace
AGENTS.md — all existing hire machinery). **Agent self-provisions:** the email
address (first send), the loop itself (prose). **Owner provides:** one forward.

**Smallest changes for the FULL zero-touch loop (both deferred, neither
blocks v0):**
- *Gap A:* accept optional `agent_id`/`agent_name` on the existing
  `POST /send-to-owner/u/{owner_id}` body; `send_to_owner` mints
  `is_primary=False` for that agent instead of Friday. ~20 lines + tests.
- *Gap B:* thread a `public` param through `mint_agent_email_address` →
  `agent_email_repo.put` (both already have the field); pick the owner-consent
  surface then. This is what makes `support@…` auto-forwardable — and the
  moment it lands, the prose rule "never send to a non-owner without approval
  on the thread" becomes load-bearing instead of redundant (it's written for
  that day already).

## Recipe 2 — Money watch  (design only)

**Ingress:** Stripe webhook via the trigger proxy (separate PR in flight —
needed because `hooks_proxy` authenticates with a bearer header Stripe won't
send). **Owner's ONE thing:** paste one URL into the Stripe dashboard.
**Fallback that works TODAY:** email receipts, manually forwarded exactly like
recipe 1 (pointing Stripe's own receipt emails at the company address needs
Gap B — Stripe's `From:` isn't the owner, so the private-address gate drops it). Listing content can ship before the
proxy lands, with the email loop as the default and the webhook as the
upgrade. **Platform gap:** none at deploy time; the agent tells the owner the
webhook URL in chat (self-serve), so listing→trigger provisioning is still
not needed — cut again.

## Recipe 3 — Break-fix  (design only)

**Ingress:** GitHub/Sentry webhooks. Neither sender can set an Authorization
header, so both also ride the trigger proxy; GitHub's `X-Hub-Signature-256`
HMAC verification is already first-class in upstream OpenClaw hooks.
**Owner's ONE thing:** paste URL + secret into repo/Sentry settings (the agent
hands them both over in chat). **Platform gap:** trigger proxy only — same
single dependency as recipe 2, already in flight.

## Where the marketplace machinery fell short (exactly)

One place, three lines: `scripts/ingest_companies_registry.py` hardcoded
`SOURCE_REPO = "paperclipai/companies"` into manifest provenance, so a
first-party package ingested from this repo would lie about its origin.
Fixed in this PR with an optional `--source-repo` flag (default unchanged).
Everything else — draft→publish transitions, safety scan, S3 artifact,
deploy plan, hire flow — expressed the recipe as-is.

## Challenge log — considered and cut

- New "recipe" listing format/schema → a one-agent `agentcompanies/v1` package already expresses it.
- Deploy-time trigger provisioning (the "known missing half") → recipe 1 needs none; recipes 2–3 need only the in-flight proxy + self-serve URL handoff.
- Cron routine polling the inbox → inbound mail already wakes the agent; event-driven wins.
- Backend change for per-agent mint (Gap A) tonight → not needed for v0; named above as the one ~20-line follow-up.
- `public` inbox flag (Gap B) tonight → v0's manual forward is *better* while trust is zero: it makes third-party auto-send physically impossible.
- Package `skills/`, `TOOLS.md`, `.paperclip.yaml` env block → fleet-baked isol8 skill already documents the mail scripts; recipe needs no env; duplication drifts.
- `--category recipe` + browse chip → the company shelf renders it; add a category when there's a second shelf to disambiguate.
- Frontend activation wizard / settings surface → the agent's own first-contact email IS the activation UX.
- Test harness for prose → extended the existing ingest tests instead (package parses, deploy-plans, scans clean, provenance stamps).

**Delete this doc when recipes 2–3 ship or the approach changes** (per the
planning-docs-are-disposable rule; the PR description carries the verdicts).
