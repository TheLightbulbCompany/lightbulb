# In-Chat Marketplace Drawer — Design

Date: 2026-05-07
Status: Draft (pending user review)

## Goal

Make the marketplace a first-class surface inside `/chat` so signed-in users browse, publish, and check earnings without leaving their chat session. Replaces the current ejecting `<Link href="/marketplace">` from the chat sidebar's `GallerySection` and the modal-based `PublishAgentModal` on the Agents control panel.

The standalone marketplace at `/marketplace/*` stays unchanged — it remains the SEO surface and the only path for anonymous visitors. The drawer is an in-chat shortcut for the highest-frequency signed-in flows.

## Non-goals

- Not redesigning `/marketplace/*` standalone routes (Browse, listing detail, dashboard, buyer history all keep their current Tailwind/editorial styles).
- Not building cross-tab paid-purchase flow with `/install-status` polling. Same-tab Stripe Checkout redirect (existing pattern) is the v1 paid path.
- Not adding a first-party `/payouts/withdraw` endpoint. Cash-out hands off to the Stripe-hosted dashboard URL (existing pattern).
- Not URL-deep-linkable for v1. Drawer state is React-only. `?openMarketplace=...` deep-link is a 5-line addition later when something cross-page needs it.
- Not building edit-published-listing or take-down UIs. Backend has no edit endpoint; take-down is admin-only.
- Not building an in-drawer listing detail page. Card click opens `/marketplace/listing/[slug]` in a new tab to preserve chat session.
- Not adding seller analytics dashboards. The `Analytics` placeholder per listing in the My Listings tab is a stub.

## Visual reference

`isol8-chat-marketplace.html` at the repo root — the user-approved mock for the drawer's three views and the wizard's four steps. The mock is the source of truth for layout, copy, and interaction details. This spec describes the architecture and integration; refer to the mock for what the surfaces look like.

---

## Architecture

### Single new component, single new context

- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawer.tsx` — right-side `<aside>` overlay. Mock dimensions: `width: min(920px, 98vw)` — substantially wider than `AgentDetailPanel` (its predecessor). Slides in via `transform: translateX(100%) → 0` over 350ms (`cubic-bezier(.4, 0, .2, 1)`). Backed by a scrim (`rgba(20, 30, 25, 0.45)`) that fades in over 250ms. Click scrim to close.
- `apps/frontend/src/components/marketplace/drawer/MarketplaceDrawerProvider.tsx` — lightweight React context. Exposes `useMarketplaceDrawer().open({view, agentId?})`, `close()`, `state`. Triggers anywhere in the tree call `open(...)` without prop drilling.
- Provider wraps `<ChatLayout>` in `apps/frontend/src/app/chat/page.tsx`.
- Drawer itself is `position: fixed` and sits visually outside the layout flow.

### State shape

```ts
type DrawerView = 'browse' | 'mine' | 'publish';

interface DrawerState {
  isOpen: boolean;
  view: DrawerView;
  publishAgentId?: string;        // pre-selected agent if entering publish from a context
  prevView?: DrawerView;          // remembered so wizard back-arrow can return to mine/browse
  publishStep?: 1 | 2 | 3 | 4 | 'success';
}
```

State lives in React only (no URL params for v1). Closing the drawer with the X button or overlay click resets `publishStep` and `publishAgentId` so the next open is clean.

### CSS approach

The drawer is a discrete component with its own styling — Tailwind utilities since that's the prevailing pattern in `apps/frontend/src/components/marketplace/*`. The publish-mode header swap uses a `data-mode="publish"` attribute on the drawer root + CSS rules that hide/show header pieces (no JS show/hide → no layout flicker).

### Visual tokens

Match the mock's palette exactly:
- `--cream: #faf7f2`, `--cream-2: #f3eee5` (drawer background, tab background)
- `--ink: #1a1a1a`, `--ink-2: #5b5b5b` (text)
- `--green: #06402b`, `--green-2: #0a5a3d` (CTAs, active states)
- `--border: #e0dbd0`, `--accent: #c2a378` (gold; "Curated" badge + published agent dots)
- Body `DM Sans`, headings `Lora` (serif, italic for marketplace context labels)

The `MarketplaceSection` we shipped on the landing page already uses these tokens via globals.css. The drawer uses the same vocabulary inline (Tailwind arbitrary values + class-name aliases as needed).

---

## Drawer header

Two header configurations driven by `data-mode` on the drawer root. CSS-only swap, no JS layout flicker.

**Default mode** (viewing Browse or My Listings):
- Left: italic Lora "Marketplace" crumb + pill-shaped tab group (Browse | My listings)
- Right: green pill `+ Publish your agent` button → opens wizard at Step 1; small `Open in full page ↗` link → opens `/marketplace` in a **new tab** (preserves drawer state, gives users the SEO-able / shareable standalone surface); circular ✕ close.

**Publish mode** (`data-mode="publish"`):
- Left: back-arrow button (`← Marketplace`) returns drawer to `prevView` (Browse or My Listings — whichever opened the wizard); italic Lora "Publish an agent" subtitle; 4-dot progress indicator; "Step N of 4" label.
- Right: ✕ close only (no Publish button or full-page link in publish mode — keep focused).

Publish mode also reveals a **sticky footer bar** at the drawer bottom holding `Back` / `Cancel` / `Continue →` (or `Submit for review →` on Step 4). Footer hidden in default mode.

---

## Three views

### Browse view

Top toolbar:
- Search input (full-width left) — debounced 300ms, calls `GET /marketplace/listings/search?q=...&format=openclaw`
- "Most installed ▾" sort dropdown (right). v1 supports two orders: most-installed (default) and recently-updated.

Category chips below toolbar — exactly six per mock:
**All · Sales · Engineering · Operations · Research · Finance**

Cards (grid `auto-fill, minmax(240px, 1fr)`, gap 16px, min-height 200px):
- 38×38 rounded-square monogram avatar (per-listing palette via `agentDesign.fromListing`)
- Lora 18px name, 13px description (clamped 2 lines)
- Footer row: italic seller name (left), green pill `Hire` button (right)
- "Curated" badge (gold `--accent`, 10px caps top-right) when `seller_id == curator`
- Already-installed listings: green border (`#aac6b4`), `#f4f8f4` background, "INSTALLED" tag top-right, disabled "Hired ✓" pill in footer. Source: `useCatalog().installedSlugs`.
- **Card body click** opens `/marketplace/listing/[slug]` in a **new tab** (`target="_blank"`). Hire button click stays in-drawer.

Hire flow:
- Free listing → calls existing `useCatalog().deploy(slug)`. Card swaps to "Installing…" → "Hired ✓"; drawer auto-closes after 700ms; new agent slides into the chat sidebar with the existing `slideIn` keyframe (green flash background fading to normal); toast confirms (see Toast section).
- Paid listing → `POST /marketplace/checkout` → `window.location.href = checkout_url`. Same-tab redirect; user lands at `/deploy-success` → deploys → bounces to `/chat`. No cross-tab polling for v1.

### My Listings view

Header row (mock layout):

```
My listings                                   [ + New listing ]
2 active listings · 1,247 total installs · $0 pending payout
```

Three numeric stats (per mock — distinct from "held + lifetime"):
- **Active listings** — count where `status == "published"`, derived client-side from the listings response
- **Total installs** — sum of `install_count` across all seller's listings
- **Pending payout** — `balance_held_cents` from `GET /marketplace/payouts/dashboard`, formatted as USD

`+ New listing` button (top-right green pill) opens the Publish wizard at Step 1.

Listings list below (vertical stack, gap 10px). Each row:
- 42×42 monogram avatar
- Lora 18px name + meta line (`Updated 2d ago · 847 installs · Premium tier`; "Premium tier" or "Free" in green)
- Status badge (right): `Live` (`#e3efe6`/`#06402b`), `In review` (`#fef4e3`/`#a07520`), `Draft` (`#f3eee5`/`#5b5b5b`), or `Taken down` (cream)
- Per-row buttons: `Analytics` (stub, opens placeholder), `Edit` (disabled stub for v1), `⋯` overflow (Copy link only — Unpublish + Take down deferred)

Empty state when zero listings: cream dashed card, Lora heading "No listings yet.", body "Open chat to publish your first agent.", `+ Publish your agent` CTA → opens wizard.

**Cash-out integration** (the Gap 1 fix from brainstorm):
- Read `dashboard_url` from `/payouts/dashboard`. If null (first-time seller, never onboarded): pending-payout stat label changes to a clickable **"Set up payouts →"** action; clicking calls `POST /marketplace/payouts/onboard` and redirects to the returned `onboarding_url`. Same code path `/marketplace/dashboard` already uses today.
- If `dashboard_url` is non-null: a `Cash out →` link sits under/next to the pending-payout stat; clicking opens `dashboard_url` (Stripe-hosted) in a new tab.

Data source decision (deferred to plan): My Listings needs the seller's listings across all statuses, but `/marketplace/listings/search` filters to `status == "published"` only. Plan picks one of: (a) extend `/listings/search` with `seller_id` + `status_in` query params; (b) add a tiny `GET /marketplace/me/listings` endpoint (~30 lines). No schema changes either way.

Small `View purchases →` link at the bottom of the My Listings view points to `/marketplace/buyer` (deferred-but-discoverable for buyers who also sell).

### Publish wizard (4 steps + success)

Wizard shares the drawer panel — not a modal over the drawer. Smooth `wizFade` 260ms entrance.

**Step 1 — Pick agent.**
Lede: *"Pick which agent in your workspace you'd like to publish. We'll bundle its config, skills, and cron jobs into a deployable artifact."*

- 2-column grid (`pick-grid`) of `pick-card` rows: 36×36 monogram avatar + Lora name + DM Sans subline
- Subline shows `N skills · M crons` derived from the agent's openclaw.json (`agents.list[i].skills.length` + `agents.list[i].cron.length`)
- The `main` agent renders disabled (45% opacity, cream-2 bg) with title "main agent can't be published"
- Already-published agents flagged with the gold pub-dot but still selectable (publishes a new version)
- Selecting a card sets `wState.agent` and pre-fills Step 2's `Public name` from the agent's display name
- If the seller entered Publish via deep-link with a pre-selected `publishAgentId` (future v2), Step 1 is skipped and the wizard opens at Step 2

**Step 2 — Listing details.**
Lede: *"Help buyers find your agent. This is what they'll see in the marketplace."*

- `Public name` text input (defaults from Step 1)
- `One-line tagline` text input, `maxlength=80`, hint "Max 80 characters."
- `Description` textarea, ~88px min height, hint "Markdown allowed."
- 2-column row: `Category` dropdown (Operations / Sales / Engineering / Research / Finance / Marketing / Personal — note 7 here vs 6 chips in Browse; Personal is publishable but doesn't get its own filter chip) | `Tags` chip input (max 5; type+Enter to add; Backspace on empty input deletes the last chip; chips lowercase on add)

Validation: 'Continue' enabled when `Public name` and `One-line tagline` both have non-empty trimmed values. Description and Tags are optional.

**Step 3 — Pricing.** **(Diverges intentionally from mock — see "Mock divergences" section.)**
Lede: *"Set what your work is worth. Price is hidden from the marketplace browse view — buyers only see it at install time."*

- `Free` / `Premium tier` pill toggle (default Free)
- Free panel: cream card with note *"Free agents install instantly. No payout setup needed. You can switch this listing to a premium tier anytime."*
- Premium panel reveals:
  - `Price per install (USD)` numeric input ($1–$20, step 1, default $10), suffix "USD · one-time, perpetual license", hint "Suggested range: $5–$20. Listings over $20 require Isol8 approval."
  - Live `breakdown` card recalculating on input:
    - List price: `$X.XX`
    - Stripe processing fee (2.9% + $0.30): `−$X.XX`
    - Isol8 platform fee (15%): `−$X.XX`
    - **Your take-home per install**: `$X.XX` (in green Lora 16px)
- **No Stripe Connect block.** Replaced with one italic line: *"Earnings accrue to your seller balance. Cash out anytime from My Listings."* (lazy-Connect direction; #565)

Validation: 'Continue' always enabled when Free is selected; for Premium, enabled when price ≥ $1.

**Step 4 — Review.**
Lede: *"Here's how your listing will appear. Submit when ready — listings go live after admin moderation (usually within 24h)."*

- Preview card rendered exactly how it'll show in Browse: monogram avatar + Lora name + "by {seller_display_name}" subline + first-line description + tag chips + footer row "Bundled: N skills · M crons · K channels" + green Free/Premium tier label
- The "Bundled" line pulls from the agent's `openclaw.json` (`skills.list.length`, `cron.length`, `channels.length`)
- Legal: *"By publishing, you agree to the [Marketplace Terms](#) and confirm this agent is yours to publish. Isol8 retains the right to remove listings that violate the [Acceptable Use Policy](#)."*
- Continue button label switches to **`Submit for review →`**

**Success step (Step 5).**
- 60×60 green check circle (`#dde9d6` bg, `#06402b` mark)
- Lora 24px "Submitted for review."
- Body: *"You'll get a notification when it's live — usually within 24 hours. Until then, you can find it in **My listings → In review**."*
- `View my listings →` button → switches drawer to My Listings tab and resets wizard state.

The 3-call submit chain (`POST /marketplace/listings` → `POST /marketplace/listings/{id}/artifact-from-agent` → `POST /marketplace/listings/{id}/submit`) and the **draft-resume on partial-failure** logic from `PublishAgentModal` are ported verbatim. Don't reinvent the retry semantics — they handle real failure modes (slug 409 on create, transient 5xx on artifact upload, etc.).

---

## Toast (post-install confirmation)

Bottom-center toast appears 700ms after the drawer auto-closes on a successful free install:

```
✓ {Name} installed in your workspace.    [ Open in chat → ]
```

- Green `--green` background, white text, rounded 14px
- Slides up from `translate(-50%, 40px)` with `cubic-bezier(.34, 1.56, .64, 1)` 350ms
- "Open in chat →" sets the new agent as active in the sidebar (existing `selectAgent` event)
- Auto-dismisses after 5500ms

The new agent in the sidebar plays the existing `new-arrival` `slideIn` keyframe animation (green flash background fading to normal) — this is already the codebase's pattern when an agent is freshly installed; nothing new on the sidebar side beyond ensuring the `new-arrival` class is applied to the inserted row.

---

## Triggers / integration points

### From the chat sidebar (replaces the existing GallerySection list)

The mock replaces the current `GallerySection` curated-agents inline rail with a much smaller two-button block in the sidebar footer:

```
        Marketplace
   ┌────────────┬───────────────┐
   │  Browse    │  + Publish    │
   └────────────┴───────────────┘
```

- `Browse` → `open({ view: 'browse' })`
- `+ Publish` → `open({ view: 'publish' })`

This is a real visual change from today: the curated-agent inline rail (and its hover-info → `AgentDetailPanel` flow) is **removed entirely**. The drawer absorbs all marketplace discovery; the sidebar becomes a clean entry point only. Cascade deletes: `AgentDetailPanel.tsx`, `GalleryItemRow.tsx`, and most of `GallerySection.tsx` (the file shrinks from ~72 lines to a ~30-line two-button block).

The sidebar's "MY AGENTS" section above (the user's own provisioned agents) stays unchanged. **Published agents** in that list now render with a small gold dot (`.pub-dot`, `--accent` color) in the top-right of their avatar — visual indicator of which of the user's agents are listed in the marketplace. Source: cross-reference between `useAgents()` and `myListings()` (the latter introduced for the My Listings tab).

### From the drawer header

- **Browse / My Listings tabs**: switch view in place
- **`+ Publish your agent`** button (top-right of header in default mode): opens Publish wizard at Step 1

### From AgentsPanel

- **Per-agent Publish button**: **deleted**. The `<PublishAgentModal>` mount + related state in `AgentsPanel.tsx:216` is removed entirely. Publishing only happens from the drawer (entered from the chat sidebar's `+ Publish` button or the drawer header's `+ Publish your agent` pill).

### Future deep-link entry (out of scope for v1)

`?openMarketplace=publish&agent_id=...` — for `/marketplace/dashboard`'s eventual "Publish in chat" button, or for cross-page links from emails. ~5 lines of `useSearchParams` in the provider. Defer until a real cross-page CTA needs it.

---

## Mock divergences (intentional)

Two places where this spec deliberately differs from `isol8-chat-marketplace.html`:

1. **Step 3 omits the Stripe Connect block.** Mock requires Stripe Connect onboarding before continuing for premium-tier listings. We shipped lazy Connect in #565 (the publish gate moved entirely to Cash Out time). Re-introducing the friction at publish would un-do that work two days later. The spec replaces the Stripe block with one italic line: *"Earnings accrue to your seller balance. Cash out anytime from My Listings."* First-time onboarding happens in My Listings' "Set up payouts" branch (Gap 1 fix) — at the moment a seller actually has money to receive.
2. **My Listings stats use "active listings / total installs / pending payout"**, not "held + lifetime". This matches the mock layout exactly. Held + lifetime are derivable from the same `/payouts/dashboard` endpoint if needed elsewhere.

Everything else (drawer dimensions, header layout, view structure, card design, wizard step layout, toast, sidebar block) follows the mock as the source of truth.

---

## User flows

### Buyer flows

| Flow | Surface | Notes |
|---|---|---|
| Anonymous browse | `/marketplace`, `/marketplace/agents` | Drawer is signed-in only; standalone is required |
| Anonymous → sign up → install | listing detail "Sign in to install" → Clerk → `/deploy-success` → `/chat` | Unchanged |
| Signed-in browse from chat | open drawer → Browse view → search/filter | New |
| Signed-in browse from outside chat | `/marketplace/agents` | Stays for SEO + bookmarked links |
| Free install via drawer | drawer Hire → `useCatalog().deploy` → drawer auto-close → sidebar slide-in animation | New |
| Free install via standalone | `/marketplace/listing/[slug]` Hire button → existing flow | Unchanged |
| Paid install via drawer | drawer Hire → `POST /checkout` → `window.location` to Stripe → `/deploy-success` → `/chat` | Same backend as standalone, drawer is just the entry |
| Paid install via standalone | `/marketplace/listing/[slug]` Buy → same Stripe flow | Unchanged |
| Purchase history | `/marketplace/buyer` only | Drawer doesn't show this; "View purchases →" link from My Listings tab points here |

### Seller flows

| Flow | Surface | Notes |
|---|---|---|
| Publish a new listing | drawer Publish wizard | **Only path after PR.** AgentsPanel button + PublishAgentModal both deleted |
| First-time Connect onboarding | drawer "Set up payouts" button OR `/marketplace/dashboard` "Onboard for payouts" button | Both call `POST /payouts/onboard`. Drawer handles this case via the `dashboard_url == null` branch |
| Cash out (already onboarded) | drawer "Cash out →" OR `/marketplace/dashboard` "Open Stripe dashboard →" | Both link to the same Stripe-hosted dashboard URL |
| View my listings + earnings | drawer My Listings tab OR `/marketplace/dashboard` | **Drawer is a strict superset** — dashboard only shows balances today; drawer shows balances + listings list |
| Edit a listing | — | v2 — backend has no edit endpoint either |
| Take-down a listing | — | v2 — admin-only via `/admin/marketplace` |

### Curator flow (admin@isol8.co)

Same as seller flow. Curator's published listings power the **landing page's `MarketplaceSection`** (the `/marketplace/featured-listings` endpoint filters by curator's Clerk user_id via `core.services.curator.get_curator_user_id`). Inside the drawer, curator's listings show up in Browse the same as any seller's, with a gold "Curated" badge in the top-right of the card. The chat sidebar's `GallerySection` no longer renders curator agents inline (replaced by the 2-button block); the curator's rows are reachable via Browse instead.

### Anonymous flow

Untouched. Drawer is signed-in only. Standalone marketplace pages handle anonymous browsing, listing detail, sign-up-to-install. The PR doesn't modify any standalone route.

---

## Duplications (intentional)

Three things end up living in two places after this PR. All three are deliberate context-splitting, not technical debt.

1. **Browse**: `/marketplace/agents` (standalone) and the drawer's Browse view both let signed-in users search and install. The standalone is required for SEO + anonymous; the drawer keeps signed-in users in chat. Same backend (`/marketplace/listings/search`).
2. **Earnings display**: `/marketplace/dashboard` and the drawer's My Listings pending-payout stat both surface held balance. Same backend (`/marketplace/payouts/dashboard`).
3. **Cash-out / Connect-onboarding triggers**: `/marketplace/dashboard` and the drawer's pending-payout action both call the same backend (`/payouts/onboard` for first-time, then the Stripe-hosted dashboard URL for cash-out).

Once the drawer is in production, `/marketplace/dashboard` is functionally redundant for sellers who use chat. We don't delete it — it's the documented public seller URL and might still be linked from emails, support docs, etc. Treat it as a minimal alternate surface.

---

## Backend touches

Almost none. The drawer reuses every existing endpoint:

- `GET /marketplace/listings/search` — Browse view
- `GET /marketplace/payouts/dashboard` — earnings panel + Cash Out check
- `POST /marketplace/payouts/onboard` — first-time Connect onboarding
- `POST /marketplace/checkout` — paid Hire
- `POST /marketplace/listings` + `/{id}/artifact-from-agent` + `/{id}/submit` — publish wizard 3-call chain
- `POST /marketplace/listings/{slug}/deploy` — free Hire (via `useCatalog`)
- `GET /marketplace/me/installed-templates` — already-installed marker (via `useCatalog`)

**One possible addition**, decision deferred to plan: My Listings needs the seller's listings across all statuses, but `/marketplace/listings/search` filters to `status="published"`. Two options:

- **Option A**: Add a `seller_id` query param + a `status_in` query param to `/listings/search`. Smaller change but extends a public endpoint with auth-gated semantics.
- **Option B**: Add a tiny new `GET /marketplace/me/listings` endpoint that reads the listings table by `seller_id == auth.user_id`. ~30 lines.

Plan picks one based on a 5-min read of `marketplace_search.py`. Either way, no schema changes.

---

## Files added / changed / deleted

The drawer is logically one component but will be decomposed into per-view files at plan time to keep individual files focused and reviewable. Expected breakdown:

| File | Change | Approx LOC |
|---|---|---|
| `components/marketplace/drawer/MarketplaceDrawer.tsx` | **New** — drawer shell, header, view router | ~150 |
| `components/marketplace/drawer/MarketplaceDrawerProvider.tsx` | **New** — context + hook | ~50 |
| `components/marketplace/drawer/BrowseView.tsx` | **New** — search + filter + card grid | ~180 |
| `components/marketplace/drawer/MyListingsView.tsx` | **New** — earnings panel + listings list | ~180 |
| `components/marketplace/drawer/PublishWizard.tsx` | **New** — 4-step wizard + state machine + 3-call submit | ~280 |
| `components/marketplace/drawer/wizardSteps.tsx` | **New** — step content components (PickAgent, Details, Pricing, Review) | ~250 |
| `app/chat/page.tsx` | Wrap in `<MarketplaceDrawerProvider>`, mount `<MarketplaceDrawer />` | ~5 |
| `components/chat/GallerySection.tsx` | Shrink to a 2-button "Marketplace" block (Browse + Publish). Remove curated-agent rail. | ~-40 |
| `components/chat/AgentDetailPanel.tsx` | **Delete** (orphaned — no caller after GallerySection list goes) | -141 |
| `components/chat/GalleryItemRow.tsx` | **Delete** (orphaned with the GallerySection list) | -~80 |
| `components/control/panels/AgentsPanel.tsx` | Remove per-agent Publish button + `<PublishAgentModal>` mount + related state | ~-30 |
| `components/marketplace/PublishAgentModal.tsx` | **Delete** | -472 |
| `lib/marketplace/api.ts` | Add `searchListings(q, category, sort)` and `myListings()` (or extend `browseListings` with the params; plan picks) | ~30 |
| `hooks/useAgents.ts` (or new `usePublishedAgents`) | Add a derived `publishedSlugs` set so sidebar published-dots can render | ~15 |
| `tests/unit/components/marketplace/drawer/MarketplaceDrawer.test.tsx` | **New** | ~120 |
| `tests/unit/components/marketplace/drawer/PublishWizard.test.tsx` | **New** | ~180 |
| `tests/unit/components/marketplace/drawer/MyListingsView.test.tsx` | **New** | ~80 |
| `tests/unit/components/marketplace/PublishAgentModal.test.tsx` | **Delete** | -~430 |
| `tests/unit/components/chat/AgentDetailPanel.test.tsx` | **Delete if exists** | — |
| `tests/unit/components/chat/GallerySection.test.tsx` | Update to test the 2-button block | ~-40 |

Net diff estimate: **+650 to +850 lines after offsets**, with the PublishAgentModal deletion absorbing a large chunk.

---

## Testing

### Vitest unit tests

- `MarketplaceDrawerProvider`: open / close / view-switching state transitions; `open({ view: 'publish', agentId })` pre-fills the wizard's pick-agent state.
- `MarketplaceDrawer`:
  - Browse view: search debounce, category filter, hire-free calls `deploy`, hire-paid sets `window.location.href`.
  - My Listings view: empty state, populated state with status badges, Cash Out button when `dashboard_url == null` (onboarding branch) vs not-null.
  - Publish wizard: form validation per step, step navigation, 3-call submit happy path, retry-on-step-2-failure (resumes from `draftListingId`).
- `GallerySection`: "Browse all →" calls `open({ view: 'browse' })` instead of navigating away.
- `AgentsPanel`: no longer renders publish button or modal (tombstone test that the references are gone).

### Manual smoke (`pnpm dev`)

- Sign in, open `/chat`, click "Browse all →" — drawer opens in Browse view.
- Search for "atlas", verify results filter.
- Click Hire on a free curator listing — drawer auto-closes, agent appears in sidebar.
- Click "+ Publish your agent" — wizard opens at Step 1 with agent grid. Pick one, fill steps, submit.
- Check My Listings — newly-submitted listing shows with "In review" badge.
- Open in incognito with a fresh account that has no Connect onboarding — Cash Out button reads "Set up payouts", clicking calls onboarding flow.

---

## Out of scope

- `/install-status` polling endpoint and cross-tab paid flow (v2)
- `/payouts/withdraw` endpoint for first-party in-app withdrawal (v2)
- URL-deep-linkable drawer state (5-line addition when needed)
- Standalone marketplace reskin (separate PR if/when justified)
- Edit-published-listing flow (no backend support)
- Take-down UI (admin-only)
- In-drawer listing detail page (card click opens standalone in new tab)
- Seller analytics dashboards (`Analytics` button is a stub)
- Buyer purchase history in drawer (link out to `/marketplace/buyer`)

---

## Open questions

None blocking. All decisions made during brainstorm:

- All three views (Browse + My Listings + Publish) — matches mock.
- Replace `PublishAgentModal` entirely; drop the per-agent Publish button on AgentsPanel.
- Browse uses full marketplace catalog (`/marketplace/listings/search`); chat sidebar's GallerySection rail is **replaced** by a 2-button Marketplace block (Browse + Publish); `AgentDetailPanel` and `GalleryItemRow` are deleted.
- Same-tab redirect for paid Hire (no cross-tab polling).
- React context for drawer state (no URL params for v1).
- Card click opens standalone listing detail in new tab.
- First-time Connect onboarding handled in drawer via the existing `/payouts/onboard` endpoint.
