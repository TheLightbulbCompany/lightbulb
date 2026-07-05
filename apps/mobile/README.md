# @isol8/mobile

Native iOS app for Isol8, built with Expo + expo-router. Plan:
`docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md` (scope: end-user
surfaces only — chat, teams, control panel, marketplace **buyer** flow,
settings; no admin/ops, no seller side, no in-app billing).

## Running

```bash
pnpm install                 # from repo root
cd apps/mobile
pnpm start                   # Metro; scan the QR with Expo Go on an iPhone
pnpm ios                     # iOS simulator (macOS only)
```

## Environment

Bundle-time public config (Expo's equivalent of `NEXT_PUBLIC_*`). Defaults
point at dev.

| Variable | Purpose | Default |
|----------|---------|---------|
| `EXPO_PUBLIC_API_URL` | Backend REST API including `/api/v1` | `https://api-dev.isol8.co/api/v1` |
| `EXPO_PUBLIC_WS_URL` | Explicit WS gateway override | derived (`api` host → `ws` host) |

Prod values: `https://api.isol8.co/api/v1` / derived `wss://ws.isol8.co`.

## Checks

```bash
pnpm typecheck               # tsc --noEmit
pnpm lint                    # expo lint
pnpm export:ios              # full Metro bundle for iOS — catches RN-unresolvable imports
```

`expo export` is the strongest check available in non-mac CI; simulator/device
verification happens via Expo Go (dev) and EAS builds (Phase 8).
