import * as SecureStore from "expo-secure-store";

/**
 * Clerk token cache backed by the iOS keychain (expo-secure-store).
 * Hand-rolled rather than importing @clerk/clerk-expo/token-cache so the
 * storage posture is explicit and version-stable.
 */
export const tokenCache = {
  async getToken(key: string): Promise<string | null> {
    try {
      return await SecureStore.getItemAsync(key);
    } catch {
      // Corrupt/inaccessible keychain entry — treat as signed out rather
      // than crashing the provider.
      await SecureStore.deleteItemAsync(key).catch(() => {});
      return null;
    }
  },
  async saveToken(key: string, value: string): Promise<void> {
    return SecureStore.setItemAsync(key, value);
  },
};
