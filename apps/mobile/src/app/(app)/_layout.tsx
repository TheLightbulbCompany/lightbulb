import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import * as Notifications from "expo-notifications";
import { Redirect, Tabs } from "expo-router";
import { StyleSheet, type ColorValue } from "react-native";
import { useEffect, useState, type ComponentProps } from "react";

import { AiConsentGate } from "../../components/consent/AiConsentGate";
import { ProvisionGate } from "../../components/provision/ProvisionGate";
import { PushRegistration } from "../../components/push/PushRegistration";
import { colors, fonts } from "../../config/theme";
import { getCurrentTarget } from "../../lib/activeAgent";
import { useMobileRooms } from "../../lib/rooms";
import { badgeCount, useChannelMentions } from "../../lib/unreadBadge";
import { LiveEventsProvider } from "../../providers/live-events";

// Feather glyphs for each top-level tab, mirroring the web sidebar sections.
function tabIcon(name: ComponentProps<typeof Feather>["name"]) {
  return function TabIcon({ color, size }: { color: ColorValue; size: number }) {
    return <Feather name={name} size={size} color={color} />;
  };
}

/**
 * The unread number, Slack's way: DMs and mentions only, muted rooms excluded.
 * Renders nothing — it lives inside LiveEventsProvider (the room machine needs
 * that context) and lifts the count out to the layout, which is the component
 * that owns the Tabs.Screen options.
 *
 * It also owns the app-icon badge, because the same number belongs on both and
 * the projector sends no `badge` in its push payload.
 */
function TabBadge({ onCount }: { onCount: (total: number) => void }) {
  const { rooms } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const mentions = useChannelMentions(rooms);
  const total = badgeCount(rooms, mentions);
  useEffect(() => {
    onCount(total);
    void Notifications.setBadgeCountAsync(total).catch(() => {});
  }, [onCount, total]);
  return null;
}

// Tab shell mirroring the web sidebar's top-level sections, gated on Clerk
// auth (the mobile equivalent of the web's middleware route protection).
// LiveEventsProvider sits inside the auth gate so AppSync subscriptions only
// exist for signed-in users.
export default function AppLayout() {
  const { isSignedIn, isLoaded } = useAuth();
  const [unread, setUnread] = useState(0);
  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <LiveEventsProvider>
      {/* getCurrentTarget reads the conversation (and thread) currently on
          screen (activeAgent.ts) so the notification handler suppresses the
          banner only for a message the user is already watching arrive. */}
      <PushRegistration getCurrentTarget={getCurrentTarget} />
      {/* Above ProvisionGate, so nothing that can reach an agent — chat, the
          first-run brief, a Teams comment — renders before the user has agreed
          to their content going to a third-party AI provider (App Store
          5.1.1(i)/5.1.2(i)). */}
      <AiConsentGate>
        <ProvisionGate>
          {/* Inside the gate: an owner with no workspace yet has no rooms to
              count, and the list fetch would only 403 in a retry loop. */}
          <TabBadge onCount={setUnread} />
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
            {/* Slack's four: Home · DMs · Activity · More. An iOS tab bar does
                not fit more, so Teams, Control and Settings stay REGISTERED
                below with `href: null` — hidden from the bar, but every route,
                deep link and push target still resolves — and `more` is the
                door to them. */}
            <Tabs.Screen
              name="home"
              options={{
                title: "Home",
                tabBarIcon: tabIcon("home"),
                tabBarBadge: unread || undefined,
              }}
            />
            <Tabs.Screen
              name="dms"
              options={{ title: "DMs", tabBarIcon: tabIcon("message-square") }}
            />
            <Tabs.Screen
              name="activity"
              options={{ title: "Activity", tabBarIcon: tabIcon("at-sign") }}
            />
            <Tabs.Screen
              name="more"
              options={{ title: "More", tabBarIcon: tabIcon("more-horizontal") }}
            />
            <Tabs.Screen
              name="teams"
              // The teams tab is a nested Stack (teams/_layout.tsx); it owns its
              // own inbox + pushed issue-detail headers, so suppress the Tabs
              // header to avoid stacking two.
              options={{ href: null, title: "Teams", headerShown: false }}
            />
            <Tabs.Screen
              name="control"
              // The control tab is a nested Stack (control/_layout.tsx); it owns
              // its own per-panel headers, so suppress the Tabs header to avoid
              // stacking two.
              options={{ href: null, title: "Control", headerShown: false }}
            />
            <Tabs.Screen name="settings" options={{ href: null, title: "Settings" }} />
            {/* Legacy agent route — resolves that agent's DM and replaces itself
                with the room route. Navigation only, not a tab. */}
            <Tabs.Screen name="agent/[agentId]" options={{ href: null, title: "Chat" }} />
            {/* Conversations live in their own Stack (room/_layout.tsx) so a
                thread pushes over the room; it owns its headers, and the tab
                bar is hidden because a conversation is a full-screen detail. */}
            <Tabs.Screen
              name="room"
              options={{ href: null, headerShown: false, tabBarStyle: { display: "none" } }}
            />
          </Tabs>
        </ProvisionGate>
      </AiConsentGate>
    </LiveEventsProvider>
  );
}
