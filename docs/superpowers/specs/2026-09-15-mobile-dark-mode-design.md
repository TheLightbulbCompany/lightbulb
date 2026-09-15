# Mobile dark mode (iOS system dark) — design

Status: approved in chat 2026-09-15. Delete this file when the feature ships.

## What

The iOS app follows the phone's appearance. `app.json` already declares
`userInterfaceStyle: "automatic"`, but every colour in
`apps/mobile/src/config/theme.ts` is a fixed light hex, so the app stays white
in dark mode while native chrome (header, tab bar glass) adapts around it.
Owner's pick: **iOS system dark** — the look of Messages/Mail/Slack in dark
mode — not a reversed brand palette.

## How

1. **Tokens resolve per appearance.** Each token in `theme.ts` becomes either
   an iOS semantic colour through expo-router's `Color` API (grounds →
   `systemBackground` / `secondarySystemBackground` / `tertiarySystemBackground`;
   text → `label` / `secondaryLabel` / `tertiaryLabel`; rules →
   `separator` / `opaqueSeparator`; `placeholderText`) or, for brand and state
   colours with no system equivalent (forest accent, error/warning pairs,
   presence), a light/dark pair via React Native's `DynamicColorIOS`. The token
   names stay, so screens that use `colors.x` need no edit.
2. **String-only consumers.** A few places need a plain string and cannot take
   a dynamic colour: `@expo/ui` `presentationBackground`, `expo-image`
   `tintColor`, Reanimated colour interpolation, SVG fills, string-built colours
   (hex + alpha suffixes). Audit with grep; each either moves to a
   `useColorScheme()`-selected hex pair or drops the override.
3. **Chrome.** `StatusBar style="auto"`. `BottomSheet` drops its
   `colorScheme: light` pin and `presentationBackground` so native sheets use
   the system sheet background.
4. **Markdown, avatars, attachments** — anything with a hardcoded colour
   outside `theme.ts` (the brand-token scan in the web has no mobile twin; grep
   for `#` literals in `apps/mobile/src`) is folded into tokens.

## Verification

Every screen on the simulator in light and dark (`xcrun simctl ui <udid>
appearance dark|light`): tab roots, room, thread, session, sheets, Teams,
Control, Settings, sign-in, consent, provisioning. Contrast of text on grounds
checked by eye against iOS system apps. Jest: tests asserting literal colour
values move to asserting tokens.

## Order

After the native list feel and native controls PRs merge (same screens).
JS-only — ships as an OTA.
