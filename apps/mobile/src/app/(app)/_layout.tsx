import { Tabs } from "expo-router";

// Tab shell mirroring the web sidebar's top-level sections. Icons and the
// authenticated gateway provider arrive with Phases 1-2.
export default function AppLayout() {
  return (
    <Tabs screenOptions={{ headerShown: true }}>
      <Tabs.Screen name="chat" options={{ title: "Chat" }} />
      <Tabs.Screen name="teams" options={{ title: "Teams" }} />
      <Tabs.Screen name="control" options={{ title: "Control" }} />
      <Tabs.Screen name="settings" options={{ title: "Settings" }} />
    </Tabs>
  );
}
