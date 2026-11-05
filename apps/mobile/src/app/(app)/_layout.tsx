import { useAuth } from "@clerk/expo";
import { Redirect, Stack } from "expo-router";
import { getFocusedRouteNameFromRoute } from "expo-router/react-navigation";
import { useEffect } from "react";

import { IapProvider } from "../../components/billing/IapProvider";
import { PurchaseListener } from "../../components/billing/PurchaseListener";
import { AiConsentGate } from "../../components/consent/AiConsentGate";
import { ProvisionGate } from "../../components/provision/ProvisionGate";
import { NotificationTapRouter } from "../../components/push/NotificationTapRouter";
import { PushRegistration } from "../../components/push/PushRegistration";
import { NameGate } from "../../components/workspace/NameGate";
import { WorkspaceGate } from "../../components/workspace/WorkspaceGate";
import { StatusBar } from "expo-status-bar";
import { Text, useColorScheme } from "react-native";
import { chatPalette, fonts } from "../../config/theme";
import { getCurrentTarget } from "../../lib/activeAgent";
import { clearRoomsCache } from "../../lib/roomsCache";
import { setupWebRTC } from "../../lib/webrtc";
import { LiveEventsProvider } from "../../providers/live-events";
import { LiveProvider } from "../../providers/live";
import { LiveSheet } from "../../components/voice/LiveSheet";

// WebRTC globals and the voice-chat audio session, once, before any Live (no-op without the module).
setupWebRTC();

/**
 * Deep-link anchor. A cold start on `/(app)/room/<id>` (a tapped push) builds
 * the stack as [(tabs), room], so the native back button pops to Chats instead
 * of stranding the reader on a rootless screen — which is what the deleted
 * `HeaderBack` component existed to paper over.
 */
export const unstable_settings = { anchor: "(tabs)" };

/** The tab bar's four roots, by route name, for the shared stack header. */
const TAB_TITLES: Record<string, string> = {
  chats: "Chats",
  activity: "Activity",
  teams: "Teams",
  settings: "Settings",
};

/**
 * Everything signed-in is a NATIVE STACK, and the tab bar is its first screen.
 *
 * That ordering is the whole point: a conversation, a Teams issue or a Control
 * panel is now PUSHED over the tabs — slide-in, parallax, interactive
 * edge-swipe back and a real back button, all from UIKit — where it used to be
 * a tab switch into a hidden tab, which expo-router's JS tabs render with
 * `animation: 'none'`. Nothing below draws its own header: the four tab roots
 * share this stack's, and every other screen gets its own pushed one.
 *
 * Navigation chrome deliberately carries NO `fontFamily`: the system font is
 * what makes an iOS nav bar read as an iOS nav bar. Brand fonts stay in content.
 * Likewise no `headerStyle.backgroundColor` and no `headerShadowVisible: false`
 * — the default translucent/glass bar is the native one.
 */
export default function AppLayout() {
  // A session still waiting on Clerk's choose-organization task is "pending",
  // which reads as signed out by default — and would bounce back to sign-in.
  // WorkspaceGate's setActive({ organization }) is what completes that task.
  const { isSignedIn, isLoaded } = useAuth({ treatPendingAsSignedOut: false });
  // The one place every sign-out path (Settings, the consent decline, the
  // gates' "Sign out", an expired session) passes through: the persisted room
  // list and messages must not outlive the session that fetched them.
  // Header title/back tint and pushed-screen background read in both schemes
  // (forest green vanished on dark; cream flashed white behind a push).
  const palette = chatPalette[useColorScheme() === "dark" ? "dark" : "light"];
  const headerTint = palette.fg;
  const signedOut = isLoaded && !isSignedIn;
  useEffect(() => {
    if (signedOut) clearRoomsCache();
  }, [signedOut]);
  if (!isLoaded) return null;
  if (!isSignedIn) return <Redirect href="/(auth)/sign-in" />;

  return (
    <>
      {/* getCurrentTarget reads the conversation (and thread) currently on
          screen (activeAgent.ts) so the notification handler suppresses the
          banner only for a message the user is already watching arrive.
          Push tokens are per user, so this runs ahead of both gates. */}
      <PushRegistration getCurrentTarget={getCurrentTarget} />
      {/* Above ProvisionGate, so nothing that can reach an agent — chat, the
          first-run brief, a Teams comment — renders before the user has agreed
          to their content going to a third-party AI provider (App Store
          5.1.1(i)/5.1.2(i)). */}
      <AiConsentGate>
        {/* A person Clerk has no name for (Apple's "Hide My Email") says what to
            be called before a workspace is opened or joined. Clerk's own
            useSignInWithApple (@clerk/expo/apple) passes Apple's fullName into
            signUp.create as firstName/lastName, but Apple shares the name on
            the FIRST authorization only — a person who declined it, or whose
            first authorization predates that, still lands here. */}
        <NameGate>
        {/* Every product API 409s without an active workspace, so everything
            that calls one — live events included — sits under this. */}
        <WorkspaceGate>
          {/* One StoreKit connection shared by the listener and every paywall
              (IapProvider: useIAP ends the global connection on unmount). */}
          <IapProvider>
            {/* Links StoreKit purchases nobody on screen is handling (one
                redelivered at launch, a renewal). Needs an active workspace. */}
            <PurchaseListener />
            <LiveEventsProvider>
              {/* One Live at a time, held above every screen. */}
              <LiveProvider>
              <ProvisionGate>
                {/* Past every gate: all headers follow the navigation theme, so the
                    status bar follows the system appearance (overrides the root's
                    dark default, which the light gate screens still need). */}
                <StatusBar style="auto" />
                <Stack
                  screenOptions={{
                    headerTintColor: headerTint,
                    // Back buttons are the bare chevron (the iOS 26 look): the tab
                    // roots blank their centred title for the top-row title, so a
                    // labelled back button would otherwise read "" or "Back".
                    headerBackButtonDisplayMode: "minimal",
                    contentStyle: { backgroundColor: palette.ground },
                  }}
                >
                  {/* One header for the four tab roots, titled by whichever is
                      focused; a Stack per tab would be four files of pure chrome.
                      Chats adds its search bar and compose button to it itself,
                      and so does Teams its "+" button (both via useTabHeader).

                      large title on Chats only (first tab); fall back to the
                      standard title if it jumps, per the spec ruling. */}
                  <Stack.Screen
                    name="(tabs)"
                    options={({ route }) => ({
                      // undefined until the tab navigator has state — i.e. the first
                      // render, which is always Chats.
                      title: TAB_TITLES[getFocusedRouteNameFromRoute(route) ?? "chats"] ?? "Chats",
                      // Telegram/Signal-style: the tab's title sits in the top row,
                      // left-aligned in Satoshi, beside the tab's own buttons — not a
                      // centred title and not a large title a row further down.
                      headerTitle: "",
                      unstable_headerLeftItems: () => [
                        {
                          type: "custom",
                          hidesSharedBackground: true,
                          element: (
                            <Text
                              accessibilityRole="header"
                              maxFontSizeMultiplier={1.2}
                              style={{ fontFamily: fonts.loraMedium, fontSize: 28, lineHeight: 34, color: headerTint }}
                            >
                              {TAB_TITLES[getFocusedRouteNameFromRoute(route) ?? "chats"] ?? "Chats"}
                            </Text>
                          ),
                        },
                      ],
                      // A header search bar forces the bar translucent, which lays the
                      // screen out UNDER it. Pinned on for all four tabs, or every
                      // switch between Chats and Activity/Teams/Settings would re-lay
                      // the whole tab controller out under and back below the bar.
                      unstable_nativeProps: { headerConfig: { translucent: true } },
                      // Translucent alone draws NO material behind the title/search
                      // bar, so a scrolled row shows through with nothing under it
                      // (worst on Chats' large title + search bar). Set once here,
                      // not per-tab, so it survives Chats' useTabHeader cleanup and
                      // covers all four tabs that share this one header.
                      headerBlurEffect: "systemChromeMaterial",
                    })}
                  />
                  {/* The conversation and its threads. Both set their own title from
                      the room (`Stack.Screen` inside the screen body). */}
                  <Stack.Screen name="room/[roomId]/index" options={{ title: "" }} />
                  <Stack.Screen name="room/[roomId]/thread/[messageId]" options={{ title: "Thread" }} />
                  <Stack.Screen name="room/[roomId]/details" options={{ title: "Details" }} />
                  {/* A super-agent automation and its run history. */}
                  <Stack.Screen name="automation/[id]" options={{ title: "Automation" }} />
                  {/* A shared Claude / Codex session's transcript: workspace-scoped, not
                      a room's (it sets its own title from the session). */}
                  <Stack.Screen name="session/new" options={{ title: "New session" }} />
                  <Stack.Screen name="session/[sessionId]" options={{ title: "Session" }} />
                  {/* Teams' detail screens and Control were nested Stacks inside
                      hidden tabs; folded in here as pushed screens, over
                      whichever tab is showing. Teams itself is a tab now (see
                      (tabs)/teams.tsx), so no bare "teams/index" screen lives
                      here any more. */}
                  <Stack.Screen name="teams/issue/[id]" options={{ title: "Issue" }} />
                  <Stack.Screen name="teams/run/[runId]" options={{ title: "Run" }} />
                  <Stack.Screen name="teams/routines" options={{ title: "Routines" }} />
                  <Stack.Screen name="control/index" options={{ title: "Control" }} />
                  <Stack.Screen name="control/overview" options={{ title: "Overview" }} />
                  <Stack.Screen name="control/agents" options={{ title: "Agents" }} />
                  <Stack.Screen name="control/agent-detail" options={{ title: "Agent" }} />
                  <Stack.Screen name="control/skills" options={{ title: "Skills" }} />
                  <Stack.Screen name="control/usage" options={{ title: "Usage" }} />
                  <Stack.Screen name="control/llm" options={{ title: "Model & Credits" }} />
                  <Stack.Screen name="control/integrations" options={{ title: "Integrations" }} />
                  <Stack.Screen name="control/website-signins" options={{ title: "Website sign-ins" }} />
                  <Stack.Screen name="control/marketplace" options={{ title: "Marketplace" }} />
                  <Stack.Screen name="control/listing/[id]" options={{ title: "Listing" }} />
                  <Stack.Screen name="workspaces" options={{ title: "Switch workspace" }} />
                  <Stack.Screen name="invite" options={{ title: "Invite people" }} />
                  {/* Legacy agent route — resolves that agent's DM and replaces
                      itself with the room route. */}
                  <Stack.Screen name="agent/[agentId]" options={{ title: "Chat" }} />
                </Stack>
                {/* After the Stack, inside the gates: a tap may only navigate once
                    this Stack exists (see NotificationTapRouter). */}
                <NotificationTapRouter />
                <LiveSheet />
              </ProvisionGate>
              </LiveProvider>
            </LiveEventsProvider>
          </IapProvider>
        </WorkspaceGate>
        </NameGate>
      </AiConsentGate>
    </>
  );
}
