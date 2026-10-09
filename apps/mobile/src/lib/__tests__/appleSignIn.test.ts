// Apple shares a person's name on the FIRST authorization only. These pin that
// it reaches Clerk, and that a missing or refused name never costs the sign-in.
const mockSignInAsync = jest.fn();
jest.mock("expo-apple-authentication", () => ({
  signInAsync: (...args: unknown[]) => mockSignInAsync(...args),
  AppleAuthenticationScope: { FULL_NAME: 0, EMAIL: 1 },
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "nonce-1" }));

import { appleName, keepAppleName, signInWithApple } from "../appleSignIn";

type Flow = Parameters<typeof signInWithApple>;
const flow = (status: string) => {
  const signIn = { create: jest.fn(async () => undefined), firstFactorVerification: { status }, createdSessionId: "sess_in" };
  const signUp = { create: jest.fn(async () => undefined), createdSessionId: "sess_up" };
  return { signIn, signUp, args: [signIn, signUp] as unknown as Flow };
};
const credential = (fullName: object | null) => ({ identityToken: "tok", fullName });

beforeEach(() => mockSignInAsync.mockReset());

it("reads Apple's name, and treats a blank one as none", () => {
  expect(appleName({ givenName: " Sam ", familyName: "Rivera" } as never)).toEqual({ firstName: "Sam", lastName: "Rivera" });
  expect(appleName({ givenName: "Sam", familyName: null } as never)).toEqual({ firstName: "Sam" });
  expect(appleName({ givenName: null, familyName: "Rivera" } as never)).toBeNull();
  expect(appleName(null)).toBeNull();
});

it("creates a new account WITH the name Apple shared", async () => {
  mockSignInAsync.mockResolvedValue(credential({ givenName: "Sam", familyName: "Rivera" }));
  const { signIn, signUp, args } = flow("transferable");
  const result = await signInWithApple(...args);
  expect(signIn.create).toHaveBeenCalledWith({ strategy: "oauth_token_apple", token: "tok" });
  expect(signUp.create).toHaveBeenCalledWith({ transfer: true, firstName: "Sam", lastName: "Rivera" });
  expect(result).toEqual({ createdSessionId: "sess_up", name: { firstName: "Sam", lastName: "Rivera" } });
});

it("still creates the account when Clerk refuses the name", async () => {
  mockSignInAsync.mockResolvedValue(credential({ givenName: "Sam", familyName: null }));
  const { signUp, args } = flow("transferable");
  signUp.create.mockRejectedValueOnce(new Error("first_name is not a valid parameter"));
  expect((await signInWithApple(...args)).createdSessionId).toBe("sess_up");
  expect(signUp.create).toHaveBeenLastCalledWith({ transfer: true });
});

it("signs a returning person in with no name from Apple and no sign-up", async () => {
  mockSignInAsync.mockResolvedValue(credential(null));
  const { signUp, args } = flow("verified");
  expect(await signInWithApple(...args)).toEqual({ createdSessionId: "sess_in", name: null });
  expect(signUp.create).not.toHaveBeenCalled();
});

it("lets a cancelled Apple sheet through as the error the screen keeps quiet about", async () => {
  mockSignInAsync.mockRejectedValue(Object.assign(new Error("cancelled"), { code: "ERR_REQUEST_CANCELED" }));
  const { signIn, args } = flow("verified");
  await expect(signInWithApple(...args)).rejects.toMatchObject({ code: "ERR_REQUEST_CANCELED" });
  expect(signIn.create).not.toHaveBeenCalled();
});

describe("keepAppleName", () => {
  const name = { firstName: "Sam", lastName: "Rivera" };
  it("writes Apple's name onto a Clerk user that has none", async () => {
    const user = { firstName: null, update: jest.fn(async () => undefined) };
    await keepAppleName(user, name);
    expect(user.update).toHaveBeenCalledWith(name);
  });
  it("never overwrites a name, and does nothing without one from Apple", async () => {
    const named = { firstName: "Ada", update: jest.fn(async () => undefined) };
    const nameless = { firstName: null, update: jest.fn(async () => undefined) };
    await keepAppleName(named, name);
    await keepAppleName(nameless, null);
    await keepAppleName(null, name);
    expect(named.update).not.toHaveBeenCalled();
    expect(nameless.update).not.toHaveBeenCalled();
  });
  it("swallows a failed write: the name question asks instead", async () => {
    const user = { firstName: null, update: jest.fn(async () => Promise.reject(new Error("offline"))) };
    await expect(keepAppleName(user, name)).resolves.toBeUndefined();
  });
});
