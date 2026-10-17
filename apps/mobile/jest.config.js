module.exports = {
  preset: "jest-expo",
  setupFilesAfterEnv: ["<rootDir>/jest-setup.ts"],
  transformIgnorePatterns: [
    // pnpm nests packages under node_modules/.pnpm/<flat-name>/node_modules/<real-name>,
    // so the optional `.pnpm/<flat>/node_modules/` prefix must be skipped before the allowlist.
    // `standard-navigation` is expo-router's own dependency (expo-router/src/standard-navigation),
    // pulled in transitively by ANY `import { router } from "expo-router"` — it ships ESM ("import"
    // syntax) with no transform, so any test that reaches a real (unmocked) expo-router import fails
    // to parse without this entry. First hit importing `router` directly into MessageMarkdown.tsx,
    // a component several test files render without mocking expo-router.
    "node_modules/(?!(?:\\.pnpm/[^/]+/node_modules/)?((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg|@clerk/.*|standard-navigation|@openuidev/.*))",
    // Carried over from jest-expo's own preset (which this array otherwise
    // REPLACES, not extends): without it, babel-preset-expo's reanimated
    // babel plugin — auto-applied because the package is installed — gets
    // asked to transform its own plugin file too (it matches the
    // `react-native-reanimated` allowlist above), which hangs the whole run
    // ("Reentrant plugin detected trying to load react-native-reanimated/plugin").
    // First hit the moment any test imports BottomSheet.tsx (reanimated's
    // first real consumer in this app) — keep this if you ever touch reanimated.
    "/node_modules/react-native-reanimated/plugin/",
  ],
};
