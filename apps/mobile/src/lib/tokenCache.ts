import * as SecureStore from "expo-secure-store";

/**
 * Clerk token cache backed by the iOS keychain (expo-secure-store).
 * Hand-rolled rather than importing @clerk/expo/token-cache: Clerk's cache
 * DELETES the entry when a read throws, which is the sign-out bug the second
 * rule below exists to prevent.
 *
 * Two rules keep sessions from evaporating:
 *
 * - Writes use AFTER_FIRST_UNLOCK. The SecureStore default (WHEN_UNLOCKED)
 *   makes keychain reads FAIL while the device is locked — e.g. a background
 *   launch off a push, or the instant before Face ID resolves.
 * - A read failure is transient, NOT corruption: return null and leave the
 *   entry alone. Deleting here turned a locked-keychain blip into a permanent
 *   sign-out (the "randomly logged out" bug). A genuinely corrupt entry
 *   self-heals — the next saveToken overwrites it.
 */
export const tokenCache = {
  async getToken(key: string): Promise<string | null> {
    try {
      return await SecureStore.getItemAsync(key);
    } catch {
      return null;
    }
  },
  async saveToken(key: string, value: string): Promise<void> {
    return SecureStore.setItemAsync(key, value, {
      keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK,
    });
  },
};
