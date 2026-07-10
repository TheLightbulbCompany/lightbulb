import { useAuth } from "@clerk/clerk-expo";
import { createApiClient } from "@isol8/shared";
import Constants from "expo-constants";
import { randomUUID } from "expo-crypto";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";
import * as SecureStore from "expo-secure-store";
import { useEffect, useMemo, useRef } from "react";

import { makeAdapter } from "../../lib/adapter";
import { parseAgentDeepLink } from "../../lib/deepLink";
import { configureNotificationHandler } from "../../lib/notifications";

const DEVICE_ID_KEY = "isol8.push.deviceId";
const LAST_TOKEN_KEY = "isol8.push.lastToken";

async function getDeviceId(): Promise<string> {
  const existing = await SecureStore.getItemAsync(DEVICE_ID_KEY).catch(() => null);
  if (existing) return existing;
  const id = randomUUID();
  await SecureStore.setItemAsync(DEVICE_ID_KEY, id).catch(() => {});
  return id;
}

/**
 * Mounted inside the signed-in subtree (GatewayProvider). Requests notification
 * permission, registers the Expo push token with the backend (once per change),
 * installs the foreground-suppression handler, and routes notification taps to
 * the right agent chat. Renders nothing.
 */
export function PushRegistration({
  getCurrentAgentId,
}: {
  getCurrentAgentId: () => string | null;
}) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  // Register token (permission → getExpoPushToken → POST /devices, de-duped).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!Device.isDevice) return; // simulators can't get a real APNs token
      const settings = await Notifications.getPermissionsAsync();
      let status = settings.status;
      if (status !== "granted") {
        status = (await Notifications.requestPermissionsAsync()).status;
      }
      if (status !== "granted" || cancelled) return;

      const projectId = Constants.expoConfig?.extra?.eas?.projectId as string | undefined;
      const { data: token } = await Notifications.getExpoPushTokenAsync(
        projectId ? { projectId } : undefined,
      );
      if (cancelled || !token) return;

      const last = await SecureStore.getItemAsync(LAST_TOKEN_KEY).catch(() => null);
      if (last === token) return; // already registered this exact token
      const deviceId = await getDeviceId();
      await api.post("/devices", { token, platform: "ios", deviceId });
      await SecureStore.setItemAsync(LAST_TOKEN_KEY, token).catch(() => {});
    })().catch(() => {
      // Best-effort: never crash the app over push registration.
    });
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Foreground suppression + tap routing.
  useEffect(() => {
    configureNotificationHandler(getCurrentAgentId);
    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      const data = response.notification.request.content.data as
        | { agentId?: string }
        | undefined;
      const agentId = data?.agentId;
      if (agentId) {
        router.push(`/agent/${agentId}`);
      } else {
        // Fallback: a deep-link URL may arrive via the notification data.
        const url = (data as { url?: string } | undefined)?.url;
        const parsed = url ? parseAgentDeepLink(url) : null;
        if (parsed) router.push(`/agent/${parsed.agentId}`);
      }
    });
    return () => sub.remove();
  }, [getCurrentAgentId]);

  return null;
}
