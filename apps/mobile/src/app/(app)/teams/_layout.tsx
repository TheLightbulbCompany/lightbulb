import { Stack } from "expo-router";

import { colors, fonts } from "../../../config/theme";

// Nested Stack for the Teams tab (mirrors control/_layout.tsx). The parent
// Tabs layout sets headerShown:false on the `teams` screen so this Stack owns
// the header — the inbox stays the tab root, and issue detail is a PUSHED
// screen with a real back button, kept OUT of the tab bar (it's a Stack child,
// not a tab).
export default function TeamsLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.cream },
        headerShadowVisible: false,
        headerTitleStyle: {
          fontFamily: fonts.dmSansBold,
          fontSize: 17,
          color: colors.ink,
        },
        headerTintColor: colors.forest,
        headerBackTitleStyle: { fontFamily: fonts.dmSans },
        contentStyle: { backgroundColor: colors.cream },
      }}
    >
      <Stack.Screen name="index" options={{ title: "Teams" }} />
      <Stack.Screen name="issue/[id]" options={{ title: "Issue" }} />
      <Stack.Screen name="run/[runId]" options={{ title: "Run" }} />
      <Stack.Screen name="routines" options={{ title: "Routines" }} />
    </Stack>
  );
}
