# Mobile parity: Google OAuth + web design language — Implementation Plan

**Status:** In progress

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Bring `apps/mobile` login and visual design to parity with the web app: Google OAuth via Clerk alongside email/password, and the web's cream/forest design language across every mobile screen.

**Tech Stack:** Expo SDK 57, @clerk/clerk-expo `useSSO`, expo-auth-session + expo-web-browser, @expo-google-fonts/dm-sans + @expo-google-fonts/lora + expo-font, RN StyleSheet (no NativeWind — plan D6).

## Global Constraints

- **Design tokens are extracted from the web frontend and must match exactly:**
  - forest (primary accent/CTA): `#06402B`; green (success/highlight): `#2d8a4e`, light variant `#4a9e74`
  - cream backgrounds: page `#faf7f2`, raised/secondary `#f3efe6`, tertiary `#f0ebe0`, hover `#e8e3d9`
  - border: `#e0dbd0`; ink (text): `#1a1a1a`; muted text: `#8a8578`, secondary muted `#5c5c5c`
  - error: `#dc2626`; white `#ffffff`
  - The existing `src/config/theme.ts` values (`bubble #f0ece2`, `error #b3261e`, `errorBg #fbe9e7`) are placeholders to be REPLACED by the web values above. All hex literals live ONLY in theme.ts — screens import tokens.
- **Fonts:** DM Sans (body/UI) and Lora (display headings, e.g. the "isol8" wordmark) via `@expo-google-fonts/dm-sans` / `@expo-google-fonts/lora` + `useFonts`; splash screen holds (`expo-splash-screen` `preventAutoHideAsync`/`hideAsync`) until fonts load. System font fallback on load failure — never a blank screen.
- **Auth flows:** email/password (existing `useSignIn`) AND Google via `useSSO().startSSOFlow({ strategy: "oauth_google" })` from `@clerk/clerk-expo`; `WebBrowser.maybeCompleteAuthSession()` called at module scope of the sign-in screen; redirect via `AuthSession.makeRedirectUri()` from `expo-auth-session`. Handle: `createdSessionId` → `setActive`; incomplete SSO status → readable error; user-cancelled browser → silent no-op (no error banner).
- **No new backend/web changes.** `apps/frontend` and `apps/backend` are read-only for this plan.
- Existing behavior contracts must not regress: cumulative-chunk segment model in `agent/[agentId].tsx`, `run_ended`-gated composer, auth guards on `(auth)`/`(app)` groups.
- Gates per task: `pnpm typecheck` + `pnpm lint` clean in `apps/mobile`; `CI=1 pnpm export:ios` bundles. Commit per task on branch `claude/isol8-mobile-app-status-79ybli`.

## Tasks

### Task 1: Design-token system + fonts + app shells

**Files:** `apps/mobile/src/config/theme.ts` (rewrite: full `colors` per Global Constraints + `fonts` (dmSans/dmSansMedium/dmSansBold/lora/loraBold post-load family names) + `type` scale (display 32/lora, title 20, body 15, caption 12) + `spacing` (4/8/12/16/24) + `radii` (8/14/18)); `apps/mobile/package.json` (add @expo-google-fonts/dm-sans, @expo-google-fonts/lora, expo-font); `src/app/_layout.tsx` (useFonts + splash hold + cream Stack background); `src/app/(app)/_layout.tsx` (Tabs: cream `#faf7f2` backgrounds, `#e0dbd0` hairline borders, forest `#06402B` active tint, `#8a8578` inactive, DM Sans labels/headers, header title styling); `src/app/(app)/teams.tsx`, `control.tsx`, `settings.tsx` (tokens + fonts on placeholders; settings keeps sign-out but styled: forest-outline or muted-destructive per web's danger pattern `#dc2626`).

Update every existing `colors.*` consumer for renamed tokens. Gates green, commit.

### Task 2: Sign-in + roster + chat restyle

**Files:** `src/app/(auth)/sign-in.tsx` (cream page, Lora "isol8" wordmark, DM Sans inputs on white with `#e0dbd0` borders + forest focus, forest `#06402B` primary CTA); `src/app/(app)/chat.tsx` (roster: cream page, white cards or hairline rows, ink names/muted ids in DM Sans, forest accents); `src/app/(app)/agent/[agentId].tsx` (assistant bubbles `#f3efe6`, user bubbles forest `#06402B` with white text, tool pills muted DM Sans caption, error text `#dc2626` on `#f3efe6`-tinted or bordered card, composer: white input, `#e0dbd0` border, forest send button, cream bar).

Preserve all behavior (segment streaming, run_ended gating, disabled states). Gates green, commit.

### Task 3: Google OAuth sign-in

**Files:** `apps/mobile/package.json` (add expo-auth-session per SDK 57); `src/app/(auth)/sign-in.tsx` (add "Continue with Google" button ABOVE the email/password block, separated by a muted "or" divider, styled: white bg, `#e0dbd0` border, ink DM Sans label, Google "G" mark drawn inline (no remote image); wire `useSSO` per Global Constraints).

Loading state on the Google button while the browser round-trip is in flight; both auth paths mutually disable while either is submitting. Gates green, commit.

### Task 4: Simulator verification (controller-run)

Boot in Expo Go: fonts load (Lora wordmark visible), cream theme on every screen, Google button opens the OAuth browser sheet and returns; email sign-in unaffected. Screenshots archived to the PR.
