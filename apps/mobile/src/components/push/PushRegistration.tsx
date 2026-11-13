import { useAuth } from "@clerk/expo";
import { createApiClient } from "@lightbulb/shared";
import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import * as SecureStore from "expo-secure-store";
import { useEffect, useMemo, useRef } from "react";
import { Platform } from "react-native";

import { makeAdapter } from "../../lib/adapter";
import { configureNotificationHandler } from "../../lib/notifications";
import { getDeviceId, LAST_REG_KEY } from "../../lib/pushDevice";

/**
 * Mounted inside the signed-in subtree (LiveEventsProvider). Requests notification
 * permission, registers the Expo push token with the backend and installs the
 * notification handler. Renders nothing. Taps are routed by
 * `NotificationTapRouter`, which has to wait for the signed-in Stack to exist —
 * this component mounts before the gates and must not navigate.
 */
export function PushRegistration() {
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
      if (Platform.OS === "android") {
        // Android 13+ shows the permission prompt only once a channel exists,
        // and Expo delivers a push with no channelId to the one named
        // "default"; HIGH importance is what makes it a heads-up banner.
        await Notifications.setNotificationChannelAsync("default", {
          name: "Messages",
          importance: Notifications.AndroidImportance.HIGH,
        });
      }
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
      await api.post("/devices", { token, platform: Platform.OS === "android" ? "android" : "ios", deviceId });
      await SecureStore.setItemAsync(LAST_REG_KEY, marker).catch(() => {});
    })().catch(() => {
      // Best-effort: never crash the app over push registration.
    });
    return () => {
      cancelled = true;
    };
  }, [api, userId]);

  useEffect(() => {
    configureNotificationHandler();
  }, []);

  return null;
}
