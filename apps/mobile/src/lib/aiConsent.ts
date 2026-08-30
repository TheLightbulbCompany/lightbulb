/**
 * Per-device record that the signed-in user agreed to their content being
 * sent to a third-party AI provider.
 *
 * Exists because of App Store guidelines 5.1.1(i) / 5.1.2(i): an app may only
 * transmit personal data to a third-party AI service after disclosing what is
 * sent, naming who receives it, and obtaining permission FIRST. Apple rejected
 * 0.1.0 (17) on exactly this and stated that carrying the disclosure in the
 * Terms/Privacy Policy alone is not sufficient — so the answer has to be a
 * blocking in-app prompt, which is what AiConsentGate renders off this latch.
 *
 * Keyed by Clerk user id, so a second account signing in on a shared device is
 * asked in its own right rather than inheriting the first user's answer.
 * Device-local on purpose: the obligation is to ask on this device before this
 * device sends anything, and a server-side record would make the answer depend
 * on a network round-trip that has to resolve before the app can be used.
 *
 * Reads FAIL CLOSED. Every other keychain reader here treats a failure as
 * transient and holds its last good value (see tokenCache), but the safe
 * default is inverted for consent: "we could not confirm you agreed" must mean
 * ask again, never proceed. Re-asking costs one tap; the other error is
 * sending someone's data on an agreement they never gave.
 */
import * as SecureStore from "expo-secure-store";

/**
 * Version suffix: bump when the disclosure itself materially changes (new
 * recipient, different data), which re-asks everyone. Renaming the key is the
 * whole mechanism — old entries are simply never read again.
 */
const KEY_PREFIX = "lightbulb.aiConsent.v1.";

/** SecureStore keys accept [A-Za-z0-9._-]; Clerk ids (`user_2ab…`) comply. */
const keyFor = (userId: string) => `${KEY_PREFIX}${userId}`;

export async function readAiConsent(userId: string): Promise<boolean> {
  try {
    return (await SecureStore.getItemAsync(keyFor(userId))) === "granted";
  } catch {
    return false;
  }
}

export async function writeAiConsent(userId: string): Promise<void> {
  await SecureStore.setItemAsync(keyFor(userId), "granted", {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK,
  });
}
