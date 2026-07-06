import { useAuth } from "@clerk/clerk-expo";
import { Redirect, Tabs } from "expo-router";

// Tab shell mirroring the web sidebar's top-level sections, gated on Clerk
// auth (the mobile equivalent of the web's middleware route protection).
export default function AppLayout() {
  const { isSignedIn, isLoaded } = useAuth();
  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <Tabs screenOptions={{ headerShown: true }}>
      <Tabs.Screen name="chat" options={{ title: "Chat" }} />
      <Tabs.Screen name="teams" options={{ title: "Teams" }} />
      <Tabs.Screen name="control" options={{ title: "Control" }} />
      <Tabs.Screen name="settings" options={{ title: "Settings" }} />
    </Tabs>
  );
}
