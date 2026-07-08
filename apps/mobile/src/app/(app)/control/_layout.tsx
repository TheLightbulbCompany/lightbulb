import { Stack } from "expo-router";

import { colors, fonts } from "../../../config/theme";

// Nested Stack for the Control tab. The parent Tabs layout sets
// headerShown:false on the `control` screen so this Stack owns the header —
// that keeps each panel a pushed screen with a real back button, and keeps
// the detail panels OUT of the tab bar (they are Stack children, not tabs).
export default function ControlLayout() {
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
      <Stack.Screen name="index" options={{ title: "Control" }} />
      <Stack.Screen name="overview" options={{ title: "Overview" }} />
      <Stack.Screen name="agents" options={{ title: "Agents" }} />
      <Stack.Screen name="agent-detail" options={{ title: "Agent" }} />
      <Stack.Screen name="skills" options={{ title: "Skills" }} />
      <Stack.Screen name="sessions" options={{ title: "Sessions" }} />
      <Stack.Screen name="channels" options={{ title: "Channels" }} />
      <Stack.Screen name="usage" options={{ title: "Usage" }} />
      <Stack.Screen name="llm" options={{ title: "Model & Credits" }} />
      <Stack.Screen name="integrations" options={{ title: "Integrations" }} />
      <Stack.Screen name="marketplace" options={{ title: "Marketplace" }} />
      <Stack.Screen name="listing/[id]" options={{ title: "Listing" }} />
    </Stack>
  );
}
