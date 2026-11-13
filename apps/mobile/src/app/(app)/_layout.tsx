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
import { clearRoomsCache } from "../../lib/roomsCache";
import { setupWebRTC } from "../../lib/webrtc";
import { LiveEventsProvider } from "../../providers/live-events";
import { LiveProvider } from "../../providers/live";
import { LiveSheet } from "../../components/voice/LiveSheet";

// WebRTC globals and the voice-chat audio session, once, before any Live (no-op without the module).
setupWebRTC();

/**
 * Deep-link anchor. A cold start on `/(app)/room/<id>` builds the stack as
 * [(tabs), room], so the native back button pops to Home instead of
 * stranding the reader on a rootless screen.
 */
export const unstable_settings = { anchor: "(tabs)" };

/** The tab bar's four roots, by route name, for the shared stack header. */
const TAB_TITLES: Record<string, string> = {
  chats: "Home",
  activity: "Needs you",
  teams: "Agent",
  settings: "Settings",
};

/**
 * Everything signed-in is a NATIVE STACK, and the tab bar is its first screen.
 *
 * That ordering is the whole point: a chat or Plans is PUSHED over the
 * tabs — slide-in, interactive edge-swipe back and a real back button, all
 * from UIKit. Nothing below draws its own header: the four tab roots
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
      {/* Push tokens are per user, so this runs ahead of both gates. */}
      <PushRegistration />
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
                      focused; a Stack per tab would be four files of pure chrome. */}
                  <Stack.Screen
                    name="(tabs)"
                    options={({ route }) => ({
                      // undefined until the tab navigator has state — i.e. the first
                      // render, which is always Home.
                      title: TAB_TITLES[getFocusedRouteNameFromRoute(route) ?? "chats"] ?? "Home",
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
                              {TAB_TITLES[getFocusedRouteNameFromRoute(route) ?? "chats"] ?? "Home"}
                            </Text>
                          ),
                        },
                      ],
                      // Translucent for all four tabs, so the screens lay out under the
                      // bar the same way (contentInsetAdjustmentBehavior keeps them clear).
                      unstable_nativeProps: { headerConfig: { translucent: true } },
                      // Translucent alone draws NO material behind the title, so a
                      // scrolled row shows through; set once for all four tabs.
                      headerBlurEffect: "systemChromeMaterial",
                    })}
                  />
                  {/* A text chat with the assistant (or a call's transcript), pushed from Home. */}
                  <Stack.Screen name="room/[roomId]/index" options={{ title: "Lightbulb" }} />
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
