import { useAuth } from "@clerk/clerk-expo";
import { createApiClient } from "@isol8/shared";
import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";
import * as SecureStore from "expo-secure-store";
import { useEffect, useMemo, useRef } from "react";

import { makeAdapter } from "../../lib/adapter";
import { parseAgentDeepLink } from "../../lib/deepLink";
import { configureNotificationHandler } from "../../lib/notifications";
import { getDeviceId, LAST_REG_KEY } from "../../lib/pushDevice";
import { sessionIdFromKey } from "../../lib/sessionKey";

/** Route a tapped notification (warm OR cold-start launch) to its agent chat,
 * landing on the session the reply belongs to (sessionKey → ?sessionId, which
 * the chat screen already consumes via useNavParamConsumer). */
function routeFromResponse(
  response: Notifications.NotificationResponse | null,
): void {
  if (!response) return;
  const data = response.notification.request.content.data as
    | { agentId?: string; sessionKey?: string; url?: string }
    | undefined;
  const agentId = data?.agentId;
  if (agentId) {
    const sessionId = sessionIdFromKey(data?.sessionKey);
    router.push(
      sessionId ? `/agent/${agentId}?sessionId=${sessionId}` : `/agent/${agentId}`,
    );
    return;
  }
  const parsed = data?.url ? parseAgentDeepLink(data.url) : null;
  if (parsed) router.push(`/agent/${parsed.agentId}`);
}

/**
 * Mounted inside the signed-in subtree (GatewayProvider). Requests notification
 * permission, registers the Expo push token with the backend, installs the
 * foreground-suppression handler, and routes notification taps (including the
 * one that cold-started the app) to the right agent chat. Renders nothing.
 */
export function PushRegistration({
  getCurrentChat,
}: {
  getCurrentChat: () => { agentId: string | null; sessionId: string | null };
}) {
  const { getToken, userId } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  // Register token (permission → getExpoPushToken → POST /devices). The de-dup
  // marker is user-scoped (`userId:token`): the Expo token is per-install and
  // identical across accounts, so keying only on the token would make a second
  // user on the same device skip registration and silently receive no pushes.
  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    (async () => {
      if (!Device.isDevice) return; // simulators can't get a real APNs token
      const settings = await Notifications.getPermissionsAsync();
      let status = settings.status;
      if (status !== "granted") {
        status = (await Notifications.requestPermissionsAsync()).status;
      }
      if (status !== "granted" || cancelled) return;

      const projectId = Constants.expoConfig?.extra?.eas?.projectId as
        | string
        | undefined;
      const { data: token } = await Notifications.getExpoPushTokenAsync(
        projectId ? { projectId } : undefined,
      );
      if (cancelled || !token) return;

      const marker = `${userId}:${token}`;
      const last = await SecureStore.getItemAsync(LAST_REG_KEY).catch(() => null);
      if (last === marker) return; // this user already registered this token
      const deviceId = await getDeviceId();
      await api.post("/devices", { token, platform: "ios", deviceId });
      await SecureStore.setItemAsync(LAST_REG_KEY, marker).catch(() => {});
    })().catch(() => {
      // Best-effort: never crash the app over push registration.
    });
    return () => {
      cancelled = true;
    };
  }, [api, userId]);

  // Tap routing. The launch (cold-start) response is delivered ONLY via
  // getLastNotificationResponseAsync — the listener below mounts too late
  // (behind async Clerk auth) to catch it, so without this a tap that
  // cold-starts the app would never open the notified agent's chat. Stable
  // deps ([]) so the listener is not re-subscribed on every render.
  useEffect(() => {
    let cancelled = false;
    Notifications.getLastNotificationResponseAsync()
      .then((r) => {
        if (!cancelled) routeFromResponse(r);
      })
      .catch(() => {});
    const sub = Notifications.addNotificationResponseReceivedListener(
      routeFromResponse,
    );
    return () => {
      cancelled = true;
      sub.remove();
    };
  }, []);

  // Foreground-suppression handler (reads the on-screen chat at handle-time).
  useEffect(() => {
    configureNotificationHandler(getCurrentChat);
  }, [getCurrentChat]);

  return null;
}
