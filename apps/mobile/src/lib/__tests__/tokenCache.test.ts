import * as SecureStore from "expo-secure-store";

import { tokenCache } from "../tokenCache";

const getItemAsync = SecureStore.getItemAsync as jest.Mock;
const setItemAsync = SecureStore.setItemAsync as jest.Mock;
const deleteItemAsync = SecureStore.deleteItemAsync as jest.Mock;

describe("tokenCache", () => {
  beforeEach(() => {
    getItemAsync.mockReset().mockResolvedValue(null);
    setItemAsync.mockReset().mockResolvedValue(undefined);
    deleteItemAsync.mockReset().mockResolvedValue(undefined);
  });

  it("returns the stored token", async () => {
    getItemAsync.mockResolvedValue("tok_123");
    await expect(tokenCache.getToken("clerk_key")).resolves.toBe("tok_123");
  });

  it("saves with AFTER_FIRST_UNLOCK so background reads work post-unlock", async () => {
    // The SecureStore default (WHEN_UNLOCKED) makes keychain reads FAIL while
    // the device is locked — e.g. a background launch off a push — which is
    // what cascaded into random sign-outs.
    await tokenCache.saveToken("clerk_key", "tok_123");
    expect(setItemAsync).toHaveBeenCalledWith("clerk_key", "tok_123", {
      keychainAccessible: "afterFirstUnlock",
    });
  });

  it("treats a read failure as transient: null, and NEVER deletes the entry", async () => {
    // A locked-keychain read error is transient. Deleting the entry here
    // turns it into a permanent sign-out — the "randomly logged out" bug.
    // Genuine corruption self-heals: the next saveToken overwrites the entry.
    getItemAsync.mockRejectedValue(new Error("User interaction is not allowed"));
    await expect(tokenCache.getToken("clerk_key")).resolves.toBeNull();
    expect(deleteItemAsync).not.toHaveBeenCalled();
  });
});
