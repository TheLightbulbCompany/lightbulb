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

- `apps/frontend/src/components/marketplace/MarketplaceDrawer.tsx` — right-side `<aside>` following the existing `AgentDetailPanel` pattern (`fixed right-0 top-0 z-50 h-full`). ~560px wide on desktop, full-width on mobile. Hosts three internal views switched by header tabs.
- `apps/frontend/src/components/marketplace/MarketplaceDrawerProvider.tsx` — lightweight React context. Exposes `useMarketplaceDrawer().open({view, agentId?})`, `close()`, `state`. Triggers anywhere in the tree call `open(...)` without prop drilling.
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

---

## Three views

### Browse view

- Top row: search input, sort dropdown ("Most installed", "Recently updated").
- Category chips: All, Sales, Engineering, Operations, Research, Finance, Marketing.
- Card grid via `GET /marketplace/listings/search?q=...&format=openclaw`. The endpoint already filters to `status="published"`; v0 catalog is curator-only but the drawer is forward-compatible for when other sellers exist.
- Card content: name, tagline (first line of description, ~120 chars), seller name, Free/Premium tier label, install button.
- **Card body click**: opens `/marketplace/listing/[slug]` in a new tab (`target="_blank"`). Preserves chat session.
- **Hire button click**:
  - Free listing → calls existing `useCatalog().deploy(slug)`. On success: drawer auto-closes after 700ms, the new agent slides into the chat sidebar (existing animation), toast confirms.
  - Paid listing → `POST /marketplace/checkout` (existing) → `window.location.href = checkoutUrl` (same-tab redirect, existing pattern). Stripe redirects buyer to `/marketplace/deploy-success` which deploys + bounces to `/chat`.
- Already-installed listings render with a green border + "Hired ✓" pill. `useCatalog().installedSlugs` is the source.

### My Listings view

- **Earnings panel** at top: held balance + lifetime earned + "Cash out →" button. Reads existing `GET /marketplace/payouts/dashboard`.
  - **First-time sellers (Connect not onboarded)**: `dashboard_url` is null. Button text is "Set up payouts" and clicks call `POST /marketplace/payouts/onboard` (existing endpoint), then redirect to the returned `onboarding_url`. Same code path as `/marketplace/dashboard` already uses today.
  - **Onboarded sellers**: button text is "Cash out →" and opens `dashboard_url` (Stripe-hosted) in a new tab.
- **Listings list** below: seller's listings across all statuses (draft, review, published, retired, taken-down).
  - Data source decision deferred to plan: either add a `seller_id` query param to the existing `/marketplace/listings/search` endpoint, OR add a small new `GET /marketplace/me/listings` endpoint. Whichever requires fewer backend changes.
  - Each row: avatar, name, status badge (Live / In review / Draft / Taken down), updated-at, install count, tier label.
  - Per-listing actions: `Analytics` (stub — opens placeholder), `Edit` (disabled stub for v1), overflow `⋯` (Copy link only — Unpublish + Take down deferred).
- **`+ New listing`** button at top right opens the Publish wizard at Step 1.
- **`View purchases →`** link at the bottom pointing to `/marketplace/buyer` (deferred-but-discoverable).

### Publish wizard view (4 steps + success)

Shares the drawer panel — not a modal over the drawer. Sticky `.md-foot` bar holds Back / Cancel / Continue buttons.

- **Step 1 — Pick agent**: grid of `useAgents()` results. The `main` agent is excluded (cannot be published; it's the user's primary chat agent). Already-published agents flagged with a small badge but still selectable (publishes a new version). If the seller entered Publish with a pre-selected `publishAgentId`, Step 1 is skipped and the wizard opens at Step 2.
- **Step 2 — Listing details**: public name (defaults to agent name), tagline (≤80 chars), description (markdown textarea, ≤4096 chars), category dropdown, tags chip input (≤5 tags). Validation matches existing `PublishAgentModal` rules.
- **Step 3 — Pricing**: Free/Premium toggle. Premium reveals price input ($1–$20 cap, matches existing backend cap), live take-home breakdown (15% platform fee + Stripe 2.9% + $0.30 processing). Earnings-accrual note: "Earnings accrue to your seller balance. Cash out anytime from My Listings."
- **Step 4 — Review**: live preview card (rendered exactly how it'll appear in Browse), bundled-skills disclosure pulled from the agent's `openclaw.json`, terms acknowledgment checkbox. Continue button label: "Submit for review →".
- **Success step**: ✓ confirmation + "Submitted for review. We'll notify you when it's live." + "View my listings →" button switches drawer to My Listings.

The 3-call submit chain (`POST /marketplace/listings` → `POST /marketplace/listings/{id}/artifact-from-agent` → `POST /marketplace/listings/{id}/submit`) and the **draft-resume on partial-failure** logic from `PublishAgentModal` are ported into the wizard verbatim. Don't reinvent the retry semantics — they handle real failure modes (slug 409 on create, transient 5xx on artifact upload, etc.).

---

## Triggers / integration points

### From the chat sidebar (`GallerySection`)

- **"Browse all →" link**: changes from `<Link href="/marketplace">` to `<button onClick={() => open({ view: 'browse' })}>`. Inline curated rows in the sidebar stay as the always-visible discovery rail.
- **GalleryItemRow's "info" button** stays — opens the existing `AgentDetailPanel` (separate from the drawer).

### From the drawer header

- **Browse / My Listings tabs**: switch view in place.
- **`+ Publish your agent`** button (top right of header in Browse + My Listings modes): opens the Publish wizard at Step 1.

### From AgentsPanel

- **Per-agent Publish button**: **deleted** (per design decision). The `<PublishAgentModal>` mount + related state in `AgentsPanel.tsx:216` is removed entirely. Publishing only happens from the drawer.

### Future deep-link entry (out of scope for v1)

`?openMarketplace=publish&agent_id=...` — for `/marketplace/dashboard`'s eventual "Publish in chat" button. ~5 lines of `useSearchParams` in the provider. Defer until a real cross-page CTA needs it.

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

Same as seller flow. Curator's published listings happen to be the data behind the chat sidebar's `GallerySection` and the landing page's `MarketplaceSection` (the curator-filter uses `seller_id == admin@isol8.co`'s Clerk user_id via `core.services.curator.get_curator_user_id`). No special path.

### Anonymous flow

Untouched. Drawer is signed-in only. Standalone marketplace pages handle anonymous browsing, listing detail, sign-up-to-install. The PR doesn't modify any standalone route.

---

## Duplications (intentional)

Three things end up living in two places after this PR. All three are deliberate context-splitting, not technical debt.

1. **Browse**: `/marketplace/agents` (standalone) and the drawer's Browse view both let signed-in users search and install. The standalone is required for SEO + anonymous; the drawer keeps signed-in users in chat. Same backend (`/marketplace/listings/search`).
2. **Earnings display**: `/marketplace/dashboard` and the drawer's My Listings earnings panel both show held + lifetime. Same backend (`/marketplace/payouts/dashboard`).
3. **Cash out trigger**: `/marketplace/dashboard` and the drawer's Cash Out button both link to the same Stripe-hosted dashboard URL.

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
| `components/chat/GallerySection.tsx` | Replace `<Link href="/marketplace">` with `<button onClick={open}>` | ~3 |
| `components/control/panels/AgentsPanel.tsx` | Remove per-agent Publish button + `<PublishAgentModal>` mount + related state | ~-30 |
| `components/marketplace/PublishAgentModal.tsx` | **Delete** | -472 |
| `lib/marketplace/api.ts` | Add `searchListings(q, category)` if not adequate, or `myListings()` for the seller-scoped fetch | ~20 |
| `tests/unit/components/marketplace/drawer/MarketplaceDrawer.test.tsx` | **New** | ~120 |
| `tests/unit/components/marketplace/drawer/PublishWizard.test.tsx` | **New** | ~180 |
| `tests/unit/components/marketplace/drawer/MyListingsView.test.tsx` | **New** | ~80 |
| `tests/unit/components/marketplace/PublishAgentModal.test.tsx` | **Delete** | -~430 |

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
- Browse uses full marketplace catalog (`/marketplace/listings/search`); GallerySection stays as sidebar discovery rail.
- Same-tab redirect for paid Hire (no cross-tab polling).
- React context for drawer state (no URL params for v1).
- Card click opens standalone listing detail in new tab.
- First-time Connect onboarding handled in drawer via the existing `/payouts/onboard` endpoint.
