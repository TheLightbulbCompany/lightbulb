import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import { Redirect, Tabs } from "expo-router";
import { StyleSheet, type ColorValue } from "react-native";
import type { ComponentProps } from "react";

import { AiConsentGate } from "../../components/consent/AiConsentGate";
import { ProvisionGate } from "../../components/provision/ProvisionGate";
import { PushRegistration } from "../../components/push/PushRegistration";
import { colors, fonts } from "../../config/theme";
import { getCurrentChat } from "../../lib/activeAgent";
import { LiveEventsProvider } from "../../providers/live-events";

// Feather glyphs for each top-level tab, mirroring the web sidebar sections.
function tabIcon(name: ComponentProps<typeof Feather>["name"]) {
  return function TabIcon({ color, size }: { color: ColorValue; size: number }) {
    return <Feather name={name} size={size} color={color} />;
  };
}

// Tab shell mirroring the web sidebar's top-level sections, gated on Clerk
// auth (the mobile equivalent of the web's middleware route protection).
// LiveEventsProvider sits inside the auth gate so AppSync subscriptions only
// exist for signed-in users.
export default function AppLayout() {
  const { isSignedIn, isLoaded } = useAuth();
  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <LiveEventsProvider>
      {/* getCurrentChat reads the chat screen's focused agent + session
          (activeAgent.ts) so the notification handler suppresses the banner for
          a reply the user is actively watching stream in. */}
      <PushRegistration getCurrentChat={getCurrentChat} />
      {/* Above ProvisionGate, so nothing that can reach an agent — chat, the
          first-run brief, a Teams comment — renders before the user has agreed
          to their content going to a third-party AI provider (App Store
          5.1.1(i)/5.1.2(i)). */}
      <AiConsentGate>
        <ProvisionGate>
          <Tabs
            screenOptions={{
              headerShown: true,
              sceneStyle: { backgroundColor: colors.cream },
              headerStyle: {
                backgroundColor: colors.cream,
                borderBottomWidth: StyleSheet.hairlineWidth,
                borderBottomColor: colors.border,
              },
              headerShadowVisible: false,
              headerTitleStyle: {
                fontFamily: fonts.sansSemiBold,
                fontSize: 17,
                color: colors.ink,
              },
              headerTintColor: colors.forest,
              tabBarStyle: {
                backgroundColor: colors.cream,
                borderTopWidth: StyleSheet.hairlineWidth,
                borderTopColor: colors.border,
              },
              tabBarActiveTintColor: colors.forest,
              tabBarInactiveTintColor: colors.muted,
              tabBarLabelStyle: { fontFamily: fonts.sansMedium, fontSize: 11 },
            }}
          >
            <Tabs.Screen
              name="chat"
              options={{ title: "Chat", tabBarIcon: tabIcon("message-circle") }}
            />
            <Tabs.Screen
              name="teams"
              // The teams tab is a nested Stack (teams/_layout.tsx); it owns its
              // own inbox + pushed issue-detail headers, so suppress the Tabs
              // header to avoid stacking two.
              options={{
                title: "Teams",
                tabBarIcon: tabIcon("inbox"),
                headerShown: false,
              }}
            />
            <Tabs.Screen
              name="control"
              // The control tab is a nested Stack (control/_layout.tsx); it owns
              // its own per-panel headers, so suppress the Tabs header to avoid
              // stacking two.
              options={{
                title: "Control",
                tabBarIcon: tabIcon("sliders"),
                headerShown: false,
              }}
            />
            <Tabs.Screen
              name="settings"
              options={{ title: "Settings", tabBarIcon: tabIcon("settings") }}
            />
            {/* Chat detail — reachable by navigation only, not a tab. */}
            <Tabs.Screen name="agent/[agentId]" options={{ href: null, title: "Chat" }} />
          </Tabs>
        </ProvisionGate>
      </AiConsentGate>
    </LiveEventsProvider>
  );
}
