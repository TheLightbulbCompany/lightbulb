# Pulse — First Run Bootstrap

This file runs once on first activation. Complete the steps below, then delete this file.

Pulse does not interrogate the user during bootstrap. Brand voice, competitors, GEO queries, subreddit monitors, social scheduling, and email platform are all click-to-connect or settings-UI configured — Pulse only mentions them when the user actually asks for something that needs them.

## Step 1: Validate Prerequisites

Required environment:
- `OPENCLAW_HOOK_TOKEN`
- `PERPLEXITY_API_KEY` — used by every research and monitoring pipeline step for structured web search via the Perplexity sonar API

Optional (click-to-connect in the Isol8 settings UI when the user needs them):
- `ADAPTLYPOST_API_KEY` — social scheduling (draft mode)
- `POSTHOG_API_KEY` — product analytics for outcome tracking
- `MAILCHIMP_API_KEY` / `RESEND_API_KEY` / `POSTMARK_API_KEY` — email platform
- `AHREFS_API_KEY` / `SEMRUSH_API_KEY` — SEO data

If the user asks Pulse to do something that requires one of the optional connections, Pulse responds per the click-to-connect pattern in AGENTS.md. Nothing prompts for these during setup.

## Step 2: Initialize Default State

Create fast-io keys with empty defaults. The settings UI (or the user's first real interaction with Pulse) populates them on demand — none are required at bootstrap.

```
pulse-config/brand-voice         → {}
pulse-config/platform-tones      → {}
pulse-config/cultural-calendar   → []
pulse-config/geo-queries         → {"queries": []}
pulse-config/competitors         → {"names": []}
pulse-config/reddit-monitors     → {"queries": []}
pulse-config/auto-publish        → {"enabled_types": [], "excluded_types": ["community", "paid_creative", "time_sensitive"]}
pulse-config/queue-limit         → {"max_per_week": 10}
pulse-state/queue-size           → {"count": 0}
pulse-state/approved-claims      → {}
pulse-state/voice-overrides      → {}
pulse-state/review-history       → []
```

Each pipeline reads these keys at the start and no-ops (or returns a click-to-connect notice to `#pulse-content`) if the required config is missing. See AGENTS.md for the per-action pattern.

## Step 3: Verify Slack Channel

Verify or request creation of:
- `#pulse-content` — drafts, Monday digest, brand mention alerts, calendar conflict warnings, community intelligence, queue status

## Step 4: Register Cron Jobs

```
openclaw cron add --name "pulse-monitoring-sweep" --cron "0 8 * * *" --tz "$USER_TIMEZONE" --session isolated --message "Run monitoring-sweep pipeline" --thinking low --light-context

openclaw cron add --name "pulse-weekly-digest" --cron "0 7 * * 1" --tz "$USER_TIMEZONE" --session isolated --message "Run weekly-digest pipeline" --thinking low --light-context

openclaw cron add --name "pulse-geo-tracking" --cron "0 6 * * 1" --tz "$USER_TIMEZONE" --session isolated --message "Run GEO Share of Model sweep" --thinking low --light-context

openclaw cron add --name "pulse-calendar-check" --cron "0 7 * * *" --tz "$USER_TIMEZONE" --session isolated --message "Run calendar conflict check on scheduled content — only alert if conflicts found" --thinking low --light-context
```

Each cron job reads its required config from fast-io at the start of its pipeline and no-ops if the config is empty.

## Step 5: Security Checks

Run `skill-vetter` against every skill in the stack. Enable `sona-security-audit` for runtime monitoring. Social scheduling skills (adaptlypost / postiz / post-bridge-social-manager) hold platform OAuth tokens — a compromised skill with posting permissions is a brand disaster. Draft mode is the default and non-negotiable.

## Step 6: Run Activation Check

Run `pulse-activation-check.js` with the current configuration piped in as JSON on stdin:

- `brand_voice` — the brand voice document (must include `adjectives`, `on_off_brand_pairs`, `tone_scales`, `anti_patterns`, `banned_phrases`, `terminology`, `audience_profiles`)
- `social_connection` — `{connected, platform, draft_mode}` for the configured social scheduler
- `email_connection` — `{connected, platform}` for the configured email platform
- `geo_queries` — array of configured GEO queries
- `competitors` — array of configured competitor entries

A missing or incomplete brand voice document is a blocker — Pulse cannot generate content without one. Missing social scheduler, email platform, GEO queries, or competitors are warnings, not blockers, since each is surfaced lazily via the click-to-connect pattern when the user first asks for something that needs it.

Exit code 0 means no blockers. Exit code 1 means a blocker — surface to the user.

## Step 7: Announce Readiness

Post to `#pulse-content`:

"Pulse is live. I run marketing intelligence autonomously — research, monitoring, GEO tracking, voice scoring, competitive intelligence — and bring you in only when human judgment is what the work actually requires. When you want me to draft content, analyze performance, or check your Share of Model, just tell me. If any required input isn't configured yet (brand voice, competitors, GEO query set, social scheduling), I'll let you know when you first ask for something that needs it — no setup paperwork up front."

## Step 8: Delete This File

Delete BOOTSTRAP.md after all steps are complete.
