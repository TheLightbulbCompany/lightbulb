# @lightbulb/mobile

Native iOS app for Isol8, built with Expo + expo-router. Scope: end-user
surfaces only — chat, teams, control panel, marketplace **buyer** flow,
settings; no admin/ops, no seller side, no in-app billing.

## Running locally — dev vs prod

```bash
pnpm install                 # from repo root
cd apps/mobile
pnpm start:dev               # Metro against DEV (api-dev.flickmeon.ai + dev Clerk)
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
| `EXPO_PUBLIC_API_URL` | Backend REST API including `/api/v1` | `https://api-dev.flickmeon.ai/api/v1` |
| `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key | required — app renders setup instructions without it |

## CI/CD + release lanes

**PR gate — `mobile-ci.yml`** (path-filtered on `apps/mobile/**` +
`packages/shared/**`): shared tests + typecheck/lint for both packages + a full
iOS Metro bundle on every PR. No secrets. Verifies only — ships nothing.

**Two CD lanes, both automated.** Which lane a change takes is decided by
compatibility, not by inspecting the diff — see *How OTA-vs-build is decided*
below.

**Lane 1 — native build → TestFlight (`mobile-release.yml`).** Trigger: push a
`mobile-v*` tag (a deliberate release) or run the workflow manually. It builds
the `production` `.ipa` **locally on the self-hosted macOS runner**
(`macos-desktop` — the same box that signs the desktop DMG), so it costs $0 and
never touches the EAS free-plan cloud-build quota, then `eas submit`s it to
TestFlight. iOS signing certs live in EAS (fetched via `EXPO_TOKEN`), so unlike
the desktop build this needs no keychain dance; the only Apple secret is the App
Store Connect API key, repo secret **`EXPO_ASC_API_KEY_BASE64`** (base64 of
`credentials/AuthKey_SZ68D5WBR2.p8`, decoded at build time to the path
`eas.json`'s submit block expects). Cut a release with:

    git tag mobile-v<version> && git push origin mobile-v<version>

First-run caveat: `eas build --local` needs `fastlane` + CocoaPods on the runner;
if that box has only ever built the desktop app, the build step is where a
missing toolchain surfaces.

**Lane 2 — JS-only OTA → production channel (`mobile-ota.yml`).** Trigger:
**every merge to `main`** touching mobile JS surfaces (native-affecting paths are
excluded). Runs `eas update --channel production` on the standard CI runner (no
Xcode needed) — installed apps pick the new JS up on next launch, no App Store
round-trip, no review. The `EXPO_PUBLIC_*` prod values are set in the workflow
because `eas update` does **not** read `eas.json`'s build-profile env; they must
stay in sync with `eas.json > build.production.env`. (Manual equivalent:
`pnpm update:production --message "…"`.)

### How OTA-vs-build is decided (runtimeVersion = fingerprint)

`app.json` sets `runtimeVersion.policy: "fingerprint"`. `@expo/fingerprint`
hashes the **native** layer — autolinked native modules, `app.json` native
config, entitlements, `eas.json` — and deliberately excludes your `app/` + `src/`
JS. That hash is baked into every native build and stamped onto every
`eas update`; `expo-updates` applies an update only when its fingerprint matches
the running binary's. So:
- **JS-only change** → identical fingerprint → OTA reaches installed apps.
- **Native change** → new fingerprint → the OTA is *not* delivered to existing
  binaries (a safe no-op), and needs a `mobile-v*` build to ship.

The `mobile-ota.yml` path filter is only a best-effort optimization to skip
pointless publishes; the fingerprint is the real, crash-proof guard. Human rule
of thumb for choosing a lane: **if `pnpm export:ios` alone can represent the
change, it's OTA; otherwise it's a `mobile-v*` build.** Switching to the
fingerprint policy retires the old `appVersion`-keyed binaries from OTA — the
first `mobile-v*` build after the switch is the new fingerprint baseline.

**EAS profiles** (`eas.json`):

| Profile | API / Clerk | Purpose |
|---------|-------------|---------|
| `development` | dev | dev client, simulator-friendly, internal |
| `preview` | dev | internal-distribution device builds (pre-TestFlight QA) |
| `production` | prod | TestFlight / App Store, auto-incrementing build number |
| `production-simulator` | prod | standalone simulator build for final founder-workspace acceptance; does not increment the store build number |

For the final shared-session acceptance pass, build after the related code and
production stacks are promoted. This bundles the production API URL and Clerk
key from `eas.json`, uses the production update channel, and runs against the
founder's production workspace. It does not need a local Metro server:

```bash
cd apps/mobile
SIM_BUILD_DIR="$(mktemp -d)"
eas build --local --platform ios --profile production-simulator --output "$SIM_BUILD_DIR/lightbulb.tar.gz"
tar -xzf "$SIM_BUILD_DIR/lightbulb.tar.gz" -C "$SIM_BUILD_DIR"
xcrun simctl install booted "$SIM_BUILD_DIR/Lightbulb.app"
```

Sign in with the founder's production account and verify the production API,
workspace runtime, session relay, and replies. A dev-client Metro session is not
this acceptance test. Simulator push registration is unavailable; record any
physical-device-only behavior separately.

Expo org **`isol8`** (project `@lightbulb/isol8-mobile`, id
`2def259d-5e79-462b-9b0f-3d01d666ba66`). CI auth is the `github-ci` robot; token
in repo secret `EXPO_TOKEN` (rotate from expo.dev → isol8 → Access tokens).
TestFlight currently ships from the personal Apple Developer account; move to the
company org account before public App Store launch.

## Push notifications

When a run **you started** finishes, your phone gets a notification — even with
the app closed — showing the agent's name and a preview of the reply; tapping it
opens that agent's chat. That is the whole of v1 (user-initiated runs only;
proactive cron / channel / Teams pushes are v2).

**Transport — Expo Push API, no APNs key in our infra.** The backend sends to
the Expo Push API (`https://exp.host/--/api/v2/push/send`); Expo/EAS fronts APNs
and manages the APNs signing key, so we hold **no APNs credential**. The Expo
Push API accepts **anonymous sends by default**, so there is **no backend secret
to set** — pushes work out of the box. (Optional anti-spoofing for later: enable
"enhanced push security" on the EAS Dashboard and attach a per-account access
token as a `Bearer` in `push_service` — a future hardening, not required.) Device
tokens live on the backend `users` row (member-scoped = org-correct), and the app
registers its token via `POST /api/v1/devices` after sign-in.

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
pnpm --filter @lightbulb/mobile test        # jest-expo runner
```

The `mobile-ci.yml` PR gate runs it alongside typecheck / lint / bundle. Native
modules the tests touch (`expo-notifications`, `expo-secure-store`, `expo-device`)
are mocked in `jest-setup.ts`.

### On-device exit gate (manual — NOT yet run)

> Push can't be exercised in CI or the simulator — it needs a physical device.
> This is the feature's exit gate and has **not been run yet** (it needs a
> TestFlight/preview build installable on a device). Run it once an EAS `preview`
> build is available.

After an EAS `preview` build (dev API/Clerk):

1. Sign in on a **physical** device; grant the notification permission prompt.
2. Send a message to an agent; **background the app**; confirm a push arrives
   showing the **agent name** (title) + **reply preview** (body).
3. **Tap** it → it lands on the conversation the message is in (a reply in a
   thread lands on the thread screen; a legacy `agentId`-only push resolves that
   agent's DM and redirects into it).
4. Repeat with **desktop and web closed (phone-only)** to exercise the teardown
   guard — the backend↔container connection must stay alive through the run so
   the completion push still fires with nothing else connected.
5. With the app **foregrounded on that conversation**, confirm **no duplicate
   banner** — the room and thread screens claim the push target
   (`claimRoomTarget` in `src/lib/activeAgent.ts`) and the handler suppresses
   only that exact room+thread. Then check the two halves of the rule: a message
   in a **different** room still banners, and a **thread reply** banners while
   you are on the parent conversation but not while you are in that thread.
6. Check the badges: a DM or an `@you` bumps the **Home tab** and the **app icon**
   (`badgeCount` — DMs and mentions only, nothing from a muted room, nothing
   from a plain channel post), and opening the room clears them. The icon badge
   is written by the running app, so a push received while the app is killed
   does not move it until launch.

## Checks

```bash
pnpm typecheck               # tsc --noEmit
pnpm lint                    # expo lint
pnpm export:ios              # full Metro bundle for iOS — catches RN-unresolvable imports
```

`expo export` is the strongest check available in non-mac CI; simulator/device
verification happens via Expo Go (dev/prod Metro) and EAS builds.
