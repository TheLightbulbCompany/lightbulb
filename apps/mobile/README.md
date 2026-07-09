# @isol8/mobile

Native iOS app for Isol8, built with Expo + expo-router. Plan:
`docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md` (scope: end-user
surfaces only — chat, teams, control panel, marketplace **buyer** flow,
settings; no admin/ops, no seller side, no in-app billing).

## Running locally — dev vs prod

```bash
pnpm install                 # from repo root
cd apps/mobile
pnpm start:dev               # Metro against DEV (api-dev.isol8.co + dev Clerk)
pnpm start:prod              # Metro against PROD (api.isol8.co + prod Clerk) — real accounts, real billing
pnpm ios                     # open in the iOS simulator (macOS only)
```

Env vars are **bundle-time** (Metro inlines them): switching dev↔prod means
stopping Metro and starting the other script — a running app can't be flipped.
When switching, sign out in the app first; the Clerk keychain session from one
instance is invalid on the other.

Inline shell vars beat `.env` files, so `start:dev`/`start:prod` always win
over whatever `.env.local` contains. Plain `pnpm start` falls back to
`.env.local` (gitignored, dev values) and then to the dev defaults baked into
`src/config/env.ts`. Clerk *publishable* keys are safe-public by design
(base64 of the instance domain) — that's why they can live in package.json.

| Variable | Purpose | Default |
|----------|---------|---------|
| `EXPO_PUBLIC_API_URL` | Backend REST API including `/api/v1` | `https://api-dev.isol8.co/api/v1` |
| `EXPO_PUBLIC_WS_URL` | Explicit WS gateway override | derived (`api` host → `ws` host) |
| `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key | required — app renders setup instructions without it |

## CI/CD + release lanes

**PR gate:** `.github/workflows/mobile-ci.yml` (path-filtered on
`apps/mobile/**` + `packages/shared/**`) runs shared tests + typecheck/lint
for both packages + a full iOS Metro bundle on every PR. No secrets needed.

**Builds (EAS):** profiles live in `eas.json` —

| Profile | API / Clerk | Purpose |
|---------|-------------|---------|
| `development` | dev | dev client, simulator-friendly, internal |
| `preview` | dev | internal-distribution device builds (pre-TestFlight QA) |
| `production` | prod | TestFlight / App Store, auto-incrementing build number |

The Expo org exists: **`isol8`** (project `@isol8/isol8-mobile`, id
`2def259d-5e79-462b-9b0f-3d01d666ba66`, linked in app.json). CI auth is the
`github-ci` robot (Developer role); its token lives in the repo secret
`EXPO_TOKEN` (rotate from expo.dev → isol8 org → Access tokens). Kick a cloud
build from the Actions tab → Mobile CI → Run workflow (profile: preview |
production), or locally with `EXPO_TOKEN=… npx eas-cli build …`. TestFlight is
currently shipping from the personal Apple Developer account; move to the
company org account before public App Store launch.

**Staying updated after release:** two lanes.
- **JS-only changes** ship over-the-air with `pnpm update:production --message
  "Fix description"` (wrapper around `eas update --channel production`) —
  installed apps pick them up on next launch, no App Store round-trip. Build 5
  is the first OTA-capable binary because it includes `expo-updates`; earlier
  TestFlight builds cannot receive OTA updates. Each `eas.json` profile is
  pinned to the matching update channel.
- **Native changes** (new native module, SDK upgrade, app.json/plugins change)
  require a new store build + TestFlight/App Store review. Rule of thumb: if
  `pnpm export:ios` alone can't represent the change, it's a store build.

## Checks

```bash
pnpm typecheck               # tsc --noEmit
pnpm lint                    # expo lint
pnpm export:ios              # full Metro bundle for iOS — catches RN-unresolvable imports
```

`expo export` is the strongest check available in non-mac CI; simulator/device
verification happens via Expo Go (dev/prod Metro) and EAS builds.
