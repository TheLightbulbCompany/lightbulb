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

## Push notifications

When a run **you started** finishes, your phone gets a notification — even with
the app closed — showing the agent's name and a preview of the reply; tapping it
opens that agent's chat. That is the whole of v1 (user-initiated runs only;
proactive cron / channel / Teams pushes are v2). Design + implementation plan:
`docs/superpowers/specs/2026-07-10-mobile-push-notifications-design.md` and
`docs/superpowers/plans/2026-07-10-mobile-push-notifications.md`.

**Transport — Expo Push API, no APNs key in our infra.** The backend sends to
the Expo Push API (`https://exp.host/--/api/v2/push/send`); Expo/EAS fronts APNs
and manages the APNs signing key, so we hold **no APNs credential**. The only
backend secret is an **Expo access token** (`EXPO_ACCESS_TOKEN`), which CDK
declares as a placeholder in AWS Secrets Manager (`isol8/${env}/expo_access_token`)
and an operator **overwrites out-of-band, per environment**, with the real token
from expo.dev (`aws secretsmanager put-secret-value`) — it is never committed.
Device tokens live on the backend `users` row (member-scoped = org-correct), and
the app registers its token via `POST /api/v1/devices` after sign-in.

**Native-build requirement.** Push needs the `expo-notifications` native module +
the `aps-environment` entitlement, both baked at **build** time — so push is a
**store/TestFlight (native) build, not an OTA update**. A `pnpm update:production`
JS-only OTA can never add it; ship it on a `preview` (device QA) or `production`
(TestFlight) EAS build. On the iOS **simulator** you can't get a real APNs token,
so registration no-ops there — use a physical device.

**Testing — `jest-expo`.** The push logic (deep-link parser, foreground-suppression
decision, `PushRegistration`) is unit-tested with `jest-expo` +
`@testing-library/react-native`, separate from the shared package's vitest suite:

```bash
pnpm --filter @isol8/mobile test        # jest-expo runner
```

The `mobile-ci.yml` PR gate runs it alongside typecheck / lint / bundle. Native
modules the tests touch (`expo-notifications`, `expo-secure-store`, `expo-device`)
are mocked in `jest-setup.ts`.

### On-device exit gate (manual — NOT yet run)

> Push can't be exercised in CI or the simulator — it needs a physical device and
> a real Expo access token in the target env's Secrets Manager. This is the
> feature's exit gate and has **not been run yet** (it needs a TestFlight/preview
> build installable on a device). Run it once an EAS `preview` build is available.

After an EAS `preview` build (dev API/Clerk) with the real `EXPO_ACCESS_TOKEN` set
in the dev secret:

1. Sign in on a **physical** device; grant the notification permission prompt.
2. Send a message to an agent; **background the app**; confirm a push arrives
   showing the **agent name** (title) + **reply preview** (body).
3. **Tap** it → it lands on that agent's chat.
4. Repeat with **desktop and web closed (phone-only)** to exercise the teardown
   guard — the backend↔container connection must stay alive through the run so
   the completion push still fires with nothing else connected.
5. (Once `getCurrentAgentId` is threaded from the chat screen) with the app
   **foregrounded on that agent's chat**, confirm **no duplicate banner**. v1
   ships `getCurrentAgentId → null` (shows all banners), so this step verifies the
   follow-up, not current behavior.

## Checks

```bash
pnpm typecheck               # tsc --noEmit
pnpm lint                    # expo lint
pnpm export:ios              # full Metro bundle for iOS — catches RN-unresolvable imports
```

`expo export` is the strongest check available in non-mac CI; simulator/device
verification happens via Expo Go (dev/prod Metro) and EAS builds.
