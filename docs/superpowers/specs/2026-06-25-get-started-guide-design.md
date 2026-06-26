# Get Started — in-app guide & activation checklist

**Date:** 2026-06-25
**Status:** Design approved (mockups signed off); awaiting spec review → implementation plan
**Surface:** `/chat` (the live app shell). No new route.

## Problem

Isol8 has a lot of conceptual surface area — two seed agents (Friday/Atlas), the
agent roster, the Company panels, the marketplace, channels, billing/providers —
and today there is **no in-app explanation of any of it**. The only "how it
works" content is a marketing component on the landing page. New users land in
`/chat` with Friday and no idea what the two agents are for, what the panels do,
or how the marketplace works.

Separately, channel setup is currently **forced** on the user (a full-screen
`BotSetupWizard` gate before they can reach chat — see "Removing the forced
channel gate"). We are replacing that friction by folding channel setup into
this guide as a normal, nudged step.

## What this is (and isn't)

A **resource-center**: a compact, re-openable guide anchored to a launcher, with
two tabs — an **activation checklist** (do) and a **concept library** (learn).
It is NOT a spotlight/coachmark tour (brittle against the in-flight IA redesign,
teaches click-paths not concepts) and NOT a docs route (a context-switch away
from the work).

Content medium: **illustrated cards** — short copy + inline SVG diagrams. No
screenshots or GIFs (they rot on every UI change; mature onboarding avoids them).

## Grounding: the real provisioning flow (why this shape)

Traced from `ProvisioningStepper.tsx` + the provision gate. Order **after this
change**:

```
Discover (landing → 3 provider cards) → /sign-up → /onboarding (personal/org)
  → Provisioning gate on /chat:  payment → provider → container → gateway
                                  → channels-sidecars → agent → ready
  → the live app  → (Get Started auto-opens; channel setup is now a step here)
```

The trailing forced `→ channels` wizard step is **removed** (see below).

**Consequence that shapes the checklist:** payment and provider choice
(ChatGPT / BYO key / Powered by Claude) happen **during provisioning, before the
container exists and before the user ever reaches `/chat`**. Therefore the
checklist must NOT contain "set up billing" or "choose a provider" — those are
already done. The guide appears **only once provisioning reaches `ready`**
(the `useProvisioningState` phase is `normal`).

## Removing the forced channel gate

Today `ProvisioningStepper` derives a `channels` phase: after the `agent` step,
if `hasMainBot(linksData)` is false (owner) or `memberLinkTarget` is set
(member), it renders `BotSetupWizard` full-screen and blocks chat until the user
sets up / skips (gated "once per owner" via localStorage). This is the screen the
user wants gone.

**Change:**
- The stepper's post-`agent` branch always resolves to `ready` — no diversion to
  `channels`. Remove the `channels` phase, its render block (the `BotSetupWizard`
  mount in the stepper), the `hasMainBot` / `findFirstUnlinkedMainProvider` /
  `memberLinkTarget` logic used solely to gate it, and the localStorage
  "show once" flag.
- **`BotSetupWizard` is NOT deleted** — it remains mounted in
  `settings/MyChannelsSection.tsx` (Settings → Channels) and
  `control/panels/AgentChannelsSection.tsx` (per-agent inspector). Channel setup
  keeps working there.
- **Member pairing** (a member linking their identity to an existing org bot)
  also relocates out of the forced gate into Settings → Channels; the guide's
  channel step covers it for members too (copy adapts).

**Trade-off (accepted):** channel setup becomes a *nudge*, not a *force*. Some
users may never set up a channel. That is the intended reduction in friction; the
checklist + launcher progress ring keep it visible.

## Surface & behavior

- **Launcher:** a floating pill/button in the bottom-right of the `/chat` main
  area, with a circular **progress ring** (e.g. `1/5`). Persists always; carries
  a gold first-run pulse until first opened.
- **Card:** a compact panel (~384px wide, max ~520px tall) anchored above the
  launcher — **not** a full-height drawer. Header (title + progress + minimize/close),
  two tabs (Checklist / Learn the basics), scrollable body.
- **Handoff rule (the key interaction):** when a checklist step performs an
  **action** (navigates somewhere), the card **minimizes back to the launcher**
  (progress ring intact) so the destination opens unobstructed; a small nudge
  bubble promises to tick the step on completion. **Reading** (opening a Learn
  article) keeps the card open. This is what resolves the collision with the
  Marketplace **Browse** drawer (a full-height right drawer) — the guide steps
  aside instead of fighting for the same space.
- **First-run:** auto-open the card once on the first `/chat` visit after
  provisioning is `ready`; dismissible. Thereafter it only opens on launcher click.
- **Coexistence:** unchanged "one right-drawer at a time" applies to Browse vs.
  the agent inspector; the Get Started card is not a right-drawer, so it never
  contends with them.

## Checklist (the "do" tab)

Each item: icon, title, one-line description, a single deep-link CTA, and a
completion checkmark. Items auto-complete from live state where cheap; the card
shows a progress ring. Core order:

| Step | CTA → destination (verified) | Auto-complete signal (verified) |
|------|------------------------------|---------------------------------|
| Meet Friday & Atlas | opens the Learn article (in-card) | article opened |
| Chat with Friday | minimize card → select Friday (`defaultId`) + focus composer | first `chat_message_sent` (fired once in `useAgentChat` `dispatchNow`, ~L1248) |
| Create your first task | minimize card → Company → Tasks, open `NewIssueDialog` (`teams/issues/NewIssueDialog`; triggered today from `InboxToolbar` "New task") | a task/issue is created (hook the dialog's create-success; fallback: issues-list count > 0) |
| Hire your first specialist | minimize card → `useMarketplaceDrawer().open({ view: "browse" })` | a marketplace-installed agent appears (`freshlyInstalledAgentId` / installed-agent marker — **not** a raw count: Atlas isn't seed-flagged, only Friday via `defaultId`) |
| Connect a channel | minimize card → `/settings?panel=channels` (Settings → Channels: `MyChannelsSection` + `BotSetupWizard`) | a main-agent bot is linked (`/channels/links/me` → `hasMainBot`) |

**Chat vs. task are distinct, intentionally:** "Chat with Friday" is the
conversational landing screen (everyone does it); "Create your first task" is the
structured issue flow the team picks up via the event model. Both are kept as
separate action steps. "Connect a channel" is a **core** step now (it replaces the
removed forced gate).

A small dashed **"set during signup ✓ Plan & billing · ✓ AI provider"**
confirmation row sits above the list (with a "Manage in Settings" link) so users
see that setup is handled and where it lives — replacing the removed steps.

### No conditional items (v1)

"Invite a teammate" is **not** a step — the org/personal choice and the initial
teammate invites already happen at `/onboarding` (the `InviteTeammatesStep`),
before chat, like billing/provider. Inviting more later lives in Settings. All
**five** steps are core and shown to everyone; the progress denominator is 5.

## Concept library (the "learn" tab)

Six evergreen, illustrated articles (~2 min each), opened **inside the card**:

1. **Meet Friday & Atlas** — the two seed agents; CEO-delegates / recruiter
   model. Includes the inline SVG org-chart (Friday → Atlas → specialists you hire).
2. **Getting around** — the roster, the Company panels, the marketplace drawer,
   Settings (maps the real surfaces).
3. **Company panels** — Dashboard, Inbox, Tasks, Routines, Goals, Projects,
   Org chart, Skills, Activity.
4. **The marketplace** — hire agents others built; publish your own.
5. **Channels** — reach agents from Slack/Discord/Telegram/WhatsApp.
6. **Billing & AI providers** — the plan and the three inference paths (reference;
   not a checklist step).

## Components (new)

Co-located under `apps/frontend/src/components/getstarted/` (flexible during
implementation):

- `GetStartedLauncher.tsx` — floating button + progress ring + first-run pulse.
- `GetStartedCard.tsx` — the panel shell, tabs, open/minimize/close state.
- `GetStartedChecklist.tsx` — the activation list (incl. the "set during signup"
  row + the conditional invite item + the deep-link/minimize handoff).
- `GetStartedLibrary.tsx` + `GetStartedArticle.tsx` — the concept index + reader.
- `getStartedContent.tsx` — the six articles as structured data (copy + inline
  SVG diagram components). Frontend-only; no backend for content.
- `useGetStarted.ts` — owns open/minimized state, derives per-step completion
  from existing hooks (`useAgents`, `useConnections`/`/channels/links/me`,
  billing, Clerk org), and persists dismissal + which articles were read.

Mounted in `ChatLayout.tsx` (launcher in the main area; gated on provisioning
phase `normal`).

## State, persistence, signals

- **Completion** is mostly **derived** from live state (roster length, channel
  links, org membership), so it stays correct without bespoke tracking.
- **Persisted** bits (survive across devices): `getStartedDismissed`,
  `getStartedSeen` (first-run), and `readArticles[]`. Stored in Clerk
  `unsafeMetadata` (same pattern as the existing `onboarded` flag).
- **"Send a task"** completion: set a flag on the first `chat_message_sent`
  (existing analytics touchpoint) rather than scanning history.

## Analytics

Via `@/lib/analytics`: `get_started_opened`, `get_started_step_clicked`
(with step id), `get_started_article_opened`, `get_started_completed`,
`get_started_dismissed`.

## Accessibility

Keyboard-openable launcher; card is a focus-trapped dialog when open; Esc closes;
the progress ring has an accessible label; SVG diagrams carry `role="img"` +
descriptive `aria-label` (already in the mockup).

## Scope

**In scope (frontend only):**
- New `getstarted/` components + `useGetStarted` hook, mounted in `ChatLayout`.
- **Edit `ProvisioningStepper.tsx`** to remove the forced `channels` phase (and
  its gating/localStorage), ending provisioning at `ready`.
- Channel/provider/billing content rides on existing surfaces (Settings, the
  marketplace drawer) via deep-links.

**Out of scope (YAGNI):**
- No new backend tables/endpoints (content + state ride on the frontend + Clerk
  metadata).
- No coachmark/spotlight engine; no screenshots/GIFs.
- No changes to the marketplace, billing, or `BotSetupWizard` internals (we only
  relocate where the wizard is *triggered* from, and keep its Settings/inspector
  mounts).
- Not rebuilt against the future 3-home IA (still a draft); concept-led content
  survives that redesign.

## Open questions for review

1. **Launcher placement** — bottom-right floating button (mockup) vs. also a
   header entry? (Recommend: floating only, to match the resource-center pattern.)
2. **Auto-open scope** — first `/chat` visit only, or also re-surface after N
   days if the checklist is unfinished? (Recommend: first visit only for v1.)
3. **Member channel pairing** — is deep-linking members to Settings → Channels
   enough, or should the guide surface a member-specific "pair your identity"
   affordance? (Recommend: Settings → Channels for v1; revisit if members get lost.)
