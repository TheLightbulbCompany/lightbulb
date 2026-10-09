/**
 * Native Sign in with Apple, keeping the person's name.
 *
 * This is Clerk's `useSignInWithApple` flow inlined, because the hook discards the
 * Apple credential and with it the one thing we need: Apple hands over `fullName`
 * on the FIRST authorization only, and never again. Someone who also chose
 * "Hide My Email" otherwise reaches us with a random relay address and no name.
 */
import type { useSignIn, useSignUp } from "@clerk/clerk-expo";
import * as AppleAuthentication from "expo-apple-authentication";
import * as Crypto from "expo-crypto";

type SignIn = NonNullable<ReturnType<typeof useSignIn>["signIn"]>;
type SignUp = NonNullable<ReturnType<typeof useSignUp>["signUp"]>;
export type AppleName = { firstName: string; lastName?: string };
type Nameable = { firstName: string | null; update: (fields: AppleName) => Promise<unknown> };

export function appleName(
  fullName: AppleAuthentication.AppleAuthenticationFullName | null | undefined,
): AppleName | null {
  const firstName = fullName?.givenName?.trim();
  if (!firstName) return null;
  const lastName = fullName?.familyName?.trim();
  return lastName ? { firstName, lastName } : { firstName };
}

/** Throws `ERR_REQUEST_CANCELED` when the person closes Apple's sheet. */
export async function signInWithApple(
  signIn: SignIn,
  signUp: SignUp,
): Promise<{ createdSessionId: string | null; name: AppleName | null }> {
  const credential = await AppleAuthentication.signInAsync({
    requestedScopes: [
      AppleAuthentication.AppleAuthenticationScope.FULL_NAME,
      AppleAuthentication.AppleAuthenticationScope.EMAIL,
    ],
    nonce: Crypto.randomUUID(),
  });
  if (!credential.identityToken) throw new Error("No identity token received from Apple.");
  const name = appleName(credential.fullName);

  await signIn.create({ strategy: "oauth_token_apple", token: credential.identityToken });
  if (signIn.firstFactorVerification.status !== "transferable") {
    return { createdSessionId: signIn.createdSessionId, name };
  }
  // A new account: create it WITH the name, so nothing ever shows it nameless. A
  // refused name must not cost the sign-up; `keepAppleName` gets a second go.
  if (name) await signUp.create({ transfer: true, ...name }).catch(() => signUp.create({ transfer: true }));
  else await signUp.create({ transfer: true });
  return { createdSessionId: signUp.createdSessionId, name };
}

/** After the session is active: Apple's name onto a Clerk user that still has none. Never throws. */
export async function keepAppleName(user: Nameable | null | undefined, name: AppleName | null) {
  if (!user || !name || user.firstName?.trim()) return;
  await user.update(name).catch(() => undefined); // NameGate asks instead
}
