import { render, screen, userEvent } from "@testing-library/react-native";
import * as SecureStore from "expo-secure-store";
import { Text } from "react-native";

// expo-secure-store is mocked globally (jest-setup.ts); these suites drive the
// jest.fn per test. Presses go through userEvent, not fireEvent: the gate is a
// ScrollView, and fireEvent doesn't reach Pressables through one (same reason
// FirstRunFunnel.test.tsx does it).
const mockSignOut = jest.fn(async () => undefined);
let mockUser: { id: string } | null = { id: "user_1" };

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }),
}));
jest.mock("@clerk/expo", () => ({
  useUser: () => ({ user: mockUser }),
  useAuth: () => ({ signOut: mockSignOut }),
}));

import { AiConsentGate } from "../AiConsentGate";

const getItem = SecureStore.getItemAsync as jest.Mock;
const setItem = SecureStore.setItemAsync as jest.Mock;

function renderGate() {
  return render(
    <AiConsentGate>
      <Text>APP</Text>
    </AiConsentGate>,
  );
}

describe("AiConsentGate", () => {
  beforeEach(() => {
    mockUser = { id: "user_1" };
    mockSignOut.mockClear();
    getItem.mockReset().mockResolvedValue(null);
    setItem.mockReset().mockResolvedValue(undefined);
  });

  // The whole point of the gate (App Store 5.1.1(i)): nothing that can reach
  // an agent may render until the user has actually answered.
  it("blocks children and shows the disclosure when no consent is stored", async () => {
    await renderGate();

    expect(await screen.findByText("Before you start")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  it("names what is sent and every provider it can be sent to", async () => {
    await renderGate();
    await screen.findByText("Before you start");

    expect(screen.getByText(/Messages you send to your agents/)).toBeTruthy();
    // All three recipients, not just the currently-selected provider — a
    // later provider switch on the web must not widen the agreement.
    expect(screen.getByText(/Amazon Web Services/)).toBeTruthy();
    expect(screen.getByText(/Anthropic — only if you connect your own Anthropic key/)).toBeTruthy();
    expect(screen.getByText(/workspace memory using AWS-managed AI models/)).toBeTruthy();
    expect(screen.queryByText(/Bedrock/)).toBeNull();
    expect(screen.getByText(/OpenAI — only if you connect/)).toBeTruthy();
    // v2 (2026-09-23): every company content can reach, and the no-training promise.
    expect(screen.getByText(/OpenRouter — routes your requests/)).toBeTruthy();
    expect(screen.getByText(/DeepInfra that run open models like DeepSeek/)).toBeTruthy();
    expect(screen.getByText(/Vercel and TypeSafe AI/)).toBeTruthy();
    expect(screen.getByText(/When Listening is on in a project: its recent messages/)).toBeTruthy();
    // The disclosure describes data flows, not plans (the app sells none).
    expect(screen.queryByText(/paid plan/i)).toBeNull();
    expect(screen.getByText(/don't store your content or use it for training/)).toBeTruthy();
  });

  it("renders children without the disclosure once consent is stored", async () => {
    getItem.mockResolvedValue("granted");

    await renderGate();

    expect(await screen.findByText("APP")).toBeTruthy();
    expect(screen.queryByText("Before you start")).toBeNull();
  });

  it("persists consent under the signed-in user's own key and reveals the app", async () => {
    const u = userEvent.setup();
    await renderGate();
    await screen.findByText("Before you start");

    await u.press(screen.getByRole("button", { name: "Agree and continue" }));

    expect(setItem).toHaveBeenCalledWith(
      "lightbulb.aiConsent.v3.user_1",
      "granted",
      expect.objectContaining({ keychainAccessible: "afterFirstUnlock" }),
    );
    expect(await screen.findByText("APP")).toBeTruthy();
  });

  // Consent is per-account, so a second user on a shared device is asked in
  // their own right rather than inheriting the first user's answer.
  it("reads a key scoped to the current user", async () => {
    mockUser = { id: "user_2" };
    getItem.mockResolvedValue(null);

    await renderGate();
    await screen.findByText("Before you start");

    expect(getItem).toHaveBeenCalledWith("lightbulb.aiConsent.v3.user_2");
  });

  // Inverted from every other keychain reader here (tokenCache holds its last
  // good value across a transient failure). "Couldn't confirm you agreed" must
  // mean ask again — never proceed.
  it("fails closed when the keychain read throws", async () => {
    getItem.mockRejectedValue(new Error("keychain locked"));

    await renderGate();

    expect(await screen.findByText("Before you start")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  // A failed write must not wave the user through: the next launch would fail
  // closed and ask again, so letting them in now would be a session of sending
  // data on an agreement that was never recorded.
  it("stays on the gate when the consent write fails", async () => {
    setItem.mockRejectedValue(new Error("keychain full"));

    const u = userEvent.setup();
    await renderGate();
    await screen.findByText("Before you start");

    await u.press(screen.getByRole("button", { name: "Agree and continue" }));

    expect(screen.queryByText("APP")).toBeNull();
    expect(screen.getByText("Before you start")).toBeTruthy();
  });

  it("declining signs the user out instead of dropping them into the app", async () => {
    const u = userEvent.setup();
    await renderGate();
    await screen.findByText("Before you start");

    await u.press(screen.getByRole("button", { name: "Don’t agree — sign out" }));

    expect(mockSignOut).toHaveBeenCalled();
    expect(screen.queryByText("APP")).toBeNull();
  });
});
