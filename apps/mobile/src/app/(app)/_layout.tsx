import { useAuth } from "@clerk/clerk-expo";
import { Redirect, Stack } from "expo-router";
import { getFocusedRouteNameFromRoute } from "expo-router/react-navigation";
import { useEffect } from "react";

import { IapProvider } from "../../components/billing/IapProvider";
import { PurchaseListener } from "../../components/billing/PurchaseListener";
import { AiConsentGate } from "../../components/consent/AiConsentGate";
import { ProvisionGate } from "../../components/provision/ProvisionGate";
import { NotificationTapRouter } from "../../components/push/NotificationTapRouter";
import { PushRegistration } from "../../components/push/PushRegistration";
import { WorkspaceGate } from "../../components/workspace/WorkspaceGate";
import { colors } from "../../config/theme";
import { getCurrentTarget } from "../../lib/activeAgent";
import { clearRoomsCache } from "../../lib/roomsCache";
import { LiveEventsProvider } from "../../providers/live-events";

/**
 * Deep-link anchor. A cold start on `/(app)/room/<id>` (a tapped push) builds
 * the stack as [(tabs), room], so the native back button pops to Home instead
 * of stranding the reader on a rootless screen — which is what the deleted
 * `HeaderBack` component existed to paper over.
 */
export const unstable_settings = { anchor: "(tabs)" };

/** The tab bar's four roots, by route name, for the shared stack header. */
const TAB_TITLES: Record<string, string> = {
  home: "Home",
  dms: "DMs",
  activity: "Activity",
  more: "More",
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
              <ProvisionGate>
                <Stack
                  screenOptions={{
                    headerTintColor: colors.forest,
                    contentStyle: { backgroundColor: colors.cream },
                  }}
                >
                  {/* One header for the four tab roots, titled by whichever is
                      focused; a Stack per tab would be four files of pure chrome.
                      Home and DMs add their search bar and compose button to it
                      themselves (RoomsScreen, via the parent navigation).

                      No large title: UIKit collapses one against a single content
                      scroll view per stack screen, and these four tabs are ONE stack
                      screen whose tab bar swaps the list underneath it — only the
                      first tab's list would ever collapse it
                      (software-mansion/react-native-screens#1997). */}
                  <Stack.Screen
                    name="(tabs)"
                    options={({ route }) => ({
                      // undefined until the tab navigator has state — i.e. the first
                      // render, which is always Home.
                      title: TAB_TITLES[getFocusedRouteNameFromRoute(route) ?? "home"] ?? "Home",
                      // A header search bar forces the bar translucent, which lays the
                      // screen out UNDER it. Pinned on for all four tabs, or every
                      // switch between a list tab and Activity/More would re-lay the
                      // whole tab controller out under and back below the bar.
                      unstable_nativeProps: { headerConfig: { translucent: true } },
                    })}
                  />
                  {/* The conversation and its threads. Both set their own title from
                      the room (`Stack.Screen` inside the screen body). */}
                  <Stack.Screen name="room/[roomId]/index" options={{ title: "" }} />
                  <Stack.Screen name="room/[roomId]/thread/[messageId]" options={{ title: "Thread" }} />
                  <Stack.Screen name="room/[roomId]/details" options={{ title: "Details" }} />
                  {/* A shared Claude / Codex session's transcript: workspace-scoped, not
                      a room's (it sets its own title from the session). */}
                  <Stack.Screen name="session/[sessionId]" options={{ title: "Session" }} />
                  {/* Teams and Control were nested Stacks inside hidden tabs; folded
                      in here, a push from More lands on a normally pushed screen. */}
                  <Stack.Screen name="teams/index" options={{ title: "Teams" }} />
                  <Stack.Screen name="teams/issue/[id]" options={{ title: "Issue" }} />
                  <Stack.Screen name="teams/run/[runId]" options={{ title: "Run" }} />
                  <Stack.Screen name="teams/routines" options={{ title: "Routines" }} />
                  {/* Every live shared session in the workspace, grouped by person. */}
                  <Stack.Screen name="sessions" options={{ title: "Shared sessions" }} />
                  <Stack.Screen name="control/index" options={{ title: "Control" }} />
                  <Stack.Screen name="control/overview" options={{ title: "Overview" }} />
                  <Stack.Screen name="control/agents" options={{ title: "Agents" }} />
                  <Stack.Screen name="control/agent-detail" options={{ title: "Agent" }} />
                  <Stack.Screen name="control/skills" options={{ title: "Skills" }} />
                  <Stack.Screen name="control/usage" options={{ title: "Usage" }} />
                  <Stack.Screen name="control/llm" options={{ title: "Model & Credits" }} />
                  <Stack.Screen name="control/integrations" options={{ title: "Integrations" }} />
                  <Stack.Screen name="control/marketplace" options={{ title: "Marketplace" }} />
                  <Stack.Screen name="control/listing/[id]" options={{ title: "Listing" }} />
                  <Stack.Screen name="settings" options={{ title: "Settings" }} />
                  <Stack.Screen name="workspaces" options={{ title: "Switch workspace" }} />
                  {/* Legacy agent route — resolves that agent's DM and replaces
                      itself with the room route. */}
                  <Stack.Screen name="agent/[agentId]" options={{ title: "Chat" }} />
                </Stack>
                {/* After the Stack, inside the gates: a tap may only navigate once
                    this Stack exists (see NotificationTapRouter). */}
                <NotificationTapRouter />
              </ProvisionGate>
            </LiveEventsProvider>
          </IapProvider>
        </WorkspaceGate>
      </AiConsentGate>
    </>
  );
}
