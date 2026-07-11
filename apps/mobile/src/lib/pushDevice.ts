import { randomUUID } from "expo-crypto";
import * as SecureStore from "expo-secure-store";

/** Stable per-install device id (persisted in the keychain). */
export const DEVICE_ID_KEY = "isol8.push.deviceId";
/** Last successful registration marker, `${userId}:${token}` — user-scoped so a
 * different account on the same device (the Expo token is per-install) still
 * re-registers instead of silently reusing the first user's registration. */
export const LAST_REG_KEY = "isol8.push.lastReg";

export async function getDeviceId(): Promise<string> {
  const existing = await SecureStore.getItemAsync(DEVICE_ID_KEY).catch(() => null);
  if (existing) return existing;
  const id = randomUUID();
  await SecureStore.setItemAsync(DEVICE_ID_KEY, id).catch(() => {});
  return id;
}

/**
 * Remove this device's push token for the signed-in user, best-effort. Called
 * at sign-out (while still authenticated) so the leaving user's replies stop
 * banner-ing on a device the next user signs into. Never throws.
 */
export async function unregisterDevice(
  del: (endpoint: string) => Promise<unknown>,
): Promise<void> {
  try {
    const deviceId = await SecureStore.getItemAsync(DEVICE_ID_KEY).catch(() => null);
    if (deviceId) await del(`/devices/${deviceId}`);
  } catch {
    // never block sign-out on cleanup
  } finally {
    await SecureStore.deleteItemAsync(LAST_REG_KEY).catch(() => {});
  }
}
