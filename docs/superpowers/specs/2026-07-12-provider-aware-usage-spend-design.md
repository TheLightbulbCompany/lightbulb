# Provider-aware usage & spend surfaces

**Date:** 2026-07-12
**Status:** Approved design, pending implementation plan

## Problem

The app shows dollar "spend" in three places, and none of them matches reality:

1. **Settings → Billing** ("Current period spend / Lifetime") and **UsagePanel** ($ cards + per-member Spend column) read the `usage-counters` DDB table via `GET /billing/account` / `GET /billing/usage`. That table is an analytics side-write (its own docstring says "authoritative billing lives in Metronome"): it records *raw* model cost without the billed markup, deliberately records **$0** for ChatGPT-OAuth and BYOK owners (while recording their tokens), and misses aux-LLM calls. A Codex or BYOK user sees "$0.00 spend" while burning millions of tokens.
2. **Teams Costs panel** (total / by-agent / by-project) and the **Dashboard "Month Spend" card** read Paperclip's `cost_events` ledger via the `/teams/costs` and `/teams/dashboard` BFF proxies. `cost_events` only captures **Paperclip-driven** runs (heartbeats, issues, routines — never webchat, channels, or cron), and only **successful** ones (the openclaw-gateway adapter omits `costUsd`/`usage` on error/timeout returns). Its dollar figure is an OpenClaw `estimatedCostUsd` passthrough, which is absent for ChatGPT-OAuth models. "Month Spend" is therefore a structural undercount of unknown size.

Meanwhile the numbers that *are* accurate have no display: Metronome (fed by the Bedrock invocation-log meter Lambda — AWS-sourced, cache-inclusive, deduped, all invocation paths) is only ever read for the credit **balance**, and OpenClaw's plan-quota data for ChatGPT-OAuth owners is never surfaced.

## Decisions (user-approved)

| Path | Display |
|------|---------|
| Bedrock managed credits | **Billed** month-to-date total + per-model split, sourced from Metronome |
| ChatGPT OAuth (flat-rate) | Plan-quota windows (5h/weekly % used, reset time) + token counts |
| BYO API key | Token/request counts only — no dollar figure |
| Free tier (trial) | Unchanged — binary cap card only; deliberately no "$X of $3" readout |
| Per-agent dollar breakdown | Not built — no honest source exists (Metronome has no agent dimension; estimates have coverage holes) |

## Scope

### 1. Removals

- **Settings BillingPanel** (`apps/frontend/src/app/settings/page.tsx`): remove the "Current period spend: $X · Lifetime: $Y" line. Subscription plan/status, trial copy, Stripe portal button, and seat/TeamBillingCard content stay.
- **UsagePanel** (`apps/frontend/src/components/control/panels/UsagePanel.tsx`): remove the "Total spend" and "Lifetime Spend" dollar cards and the per-member **Spend** column. Token grid (input/output/cache-read/cache-write), request count, and per-member token/request rows stay.
- **Teams Costs panel**: delete `apps/frontend/src/components/teams/panels/CostsPanel.tsx`, its `TEAMS_LIST_KEYS.costs` / `PANEL_LOADERS.costs` registrations, the command-palette "go-costs" action, and the backend BFF route `GET /teams/costs` (`apps/backend/routers/teams/feed.py:142`). Retire the `teams-costs` panel id via the existing retired-panel alias mechanism so stale layouts/URLs don't render a missing panel.
- `usage-counters` recording is **unchanged** — it remains the token-analytics source (and feeds token displays). Only its dollar columns lose their UI. `GET /billing/account` may keep returning `current_spend`/`lifetime_spend` for now (admin/analytics), but no user-facing component renders them.

### 2. New backend surface — `GET /billing/spend`

One provider-aware endpoint; the response carries a `mode` discriminator so the frontend renders the right variant without re-deriving provider logic.

```
{ "mode": "bedrock",
  "billed_month_to_date_cents": 1234,
  "by_model": [ { "model": "Claude Opus 4.6", "tier": "opus", "cents": 900 }, … ],
  "period_start": "2026-07-01" }

{ "mode": "oauth_quota",
  "plan": "pro",
  "windows": [ { "label": "5h", "used_percent": 43, "resets_at": "…" },
               { "label": "Week", "used_percent": 61, "resets_at": "…" } ] }

{ "mode": "tokens_only" }        // byo_key, and any degraded/fallback case
```

- **Bedrock branch:** new read method on `MetronomeClient` — `GET /v1/customers/{customer_id}/invoices?status=DRAFT` (Metronome keeps a continuously-updated draft usage invoice per billing period; line items carry `pricing_group_values` with our `tier` key). MTD figure and `by_model` split = **usage line items only** (positive-quantity items carrying a `tier` pricing-group value); commit/credit draw-down line items (negative totals, no tier) are excluded from both — the panel shows what usage cost, not the net-of-credits invoice arithmetic, since remaining credits are already shown by the balance surface. Map `tier` values to display names via the existing model catalog. Server-side cache ~60s per owner (run_in_thread, off the event loop, same pattern as the fast-gate reads). This matches the bill by construction — it is the invoice Metronome finalizes.
- **OAuth branch:** proxy the existing gateway RPC `models.authStatus` through the connection pool; read the `provider === "openai"` entry's `usage.windows[]` / `usage.plan`. OpenClaw caches upstream 60s; we add no extra cache. If the container is unreachable, the RPC fails, or the usage block is absent (upstream `wham/usage` is an unofficial endpoint), **degrade to `mode: "tokens_only"`** — never error the panel.
- **BYOK / unknown / trial:** return `mode: "tokens_only"`. Trial owners keep the existing gate-card behavior; this endpoint adds nothing for them.
- Auth: same owner-scoped auth as `/billing/usage`; org-member visibility mirrors the existing usage-panel admin gating.

### 3. Teams dashboard re-point

- `GET /teams/dashboard` (backend BFF, `routers/teams/feed.py:151`): replace the Paperclip-sourced `costs.monthSpendCents` with the Metronome billed MTD (same service function as `/billing/spend`, shared cache) **when the owner is on the Bedrock path**; omit the `costs` block otherwise. Other dashboard fields keep coming from Paperclip verbatim.
- **DashboardPanel** (`apps/frontend/src/components/teams/panels/DashboardPanel.tsx`): "Month Spend" card renders only when the costs block is present; its link target changes from the deleted Costs panel to the Usage surface (`?panel=usage`). The "% of budget" sub-label is dropped (budget lived in Paperclip's model; not carried over).

### 4. Frontend — provider-aware UsagePanel

`UsagePanel` fetches `/billing/spend` alongside the existing `/billing/usage` and renders by `mode`:

- **bedrock:** "Billed this month" total + per-model split (bar or table), with a link to credits/top-up (LLMPanel CreditsSection). Token grid below.
- **oauth_quota:** quota bars per window ("Weekly limit — 61% used · resets Tue 2:10pm"), plan badge, token grid below with "included in your ChatGPT plan" framing.
- **tokens_only:** token grid with "billed by your provider" framing for BYOK.

Placement, naming, and admin gating unchanged (Settings → Usage; dockview `control:usage`).

### 5. Drive-by fixes

- `admin_service.get_overview` reads usage period `"current"`, which `record_usage` never writes (rows are `YYYY-MM` / `lifetime` / `member:*`) → read the current `YYYY-MM` period so the admin usage view stops being permanently empty.
- `usage_service.py` docstring claims the raw-cost override covers `meta/` refs; the code gate (`_is_bedrock_model`) excludes them and the Meta trial is dead — fix the docstring.

## Error handling

- Metronome unreachable / no draft invoice yet (fresh period, brand-new customer): return `billed_month_to_date_cents: 0` with empty `by_model` when the customer exists but has no draft invoice; fall to `tokens_only` on client errors. Log + metric on failures; never surface a raw error in the panel.
- OAuth quota fetch failures degrade silently to `tokens_only` (see above). The unofficial upstream endpoint breaking must never break the Usage panel.
- Dashboard BFF: a Metronome failure omits the `costs` block (card hides) rather than failing the whole dashboard payload — dashboard already tolerates partial upstream results.

## Testing

- Backend unit: `/billing/spend` per provider path (bedrock happy-path invoice parse incl. tier→name mapping; oauth happy + degraded; byok; trial), MetronomeClient invoice read (mocked HTTP), dashboard costs re-point (bedrock vs non-bedrock), cache behavior, admin `get_overview` period fix.
- Frontend: UsagePanel per-mode render tests (single-file vitest); DashboardPanel card presence/absence; removed surfaces stay removed (no spend strings in BillingPanel).
- Live validation (dev): one Bedrock owner — compare `/billing/spend` MTD against the Metronome dashboard's draft invoice; one OAuth owner — quota bars match Codex CLI `/status`.

## Out of scope

- Per-agent/per-project dollar attribution (no honest source).
- Trial daily-spend readout (deliberate product decision to keep it binary).
- Reading BYOK providers' real billing (requires org-admin keys).
- Changes to what `usage-counters` records, the meter Lambda, or Metronome ingest/rating.
- Paperclip-side changes (cost_events keeps existing for Paperclip-internal budget features; we just stop displaying it).
