import { useAuth } from "@clerk/clerk-expo";
import { Redirect, Tabs } from "expo-router";

import { GatewayProvider } from "../../providers/gateway";

// Tab shell mirroring the web sidebar's top-level sections, gated on Clerk
// auth (the mobile equivalent of the web's middleware route protection).
// GatewayProvider sits inside the auth gate so the socket only exists for
// signed-in users.
export default function AppLayout() {
  const { isSignedIn, isLoaded } = useAuth();
  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <GatewayProvider>
      <Tabs screenOptions={{ headerShown: true }}>
        <Tabs.Screen name="chat" options={{ title: "Chat" }} />
        <Tabs.Screen name="teams" options={{ title: "Teams" }} />
        <Tabs.Screen name="control" options={{ title: "Control" }} />
        <Tabs.Screen name="settings" options={{ title: "Settings" }} />
        {/* Chat detail — reachable by navigation only, not a tab. */}
        <Tabs.Screen name="agent/[agentId]" options={{ href: null, title: "Chat" }} />
      </Tabs>
    </GatewayProvider>
  );
}
