import { render, screen } from "@testing-library/react-native";

// Per-test billing fixture the mocked api client serves (factory is lazy —
// the closure reads this at request time, after each test assigns it).
let mockAccountFixture: Record<string, unknown>;

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: jest.fn() }),
  useUser: () => ({
    user: {
      id: "user_1",
      fullName: "Test User",
      primaryEmailAddress: { emailAddress: "test@example.com" },
    },
  }),
  useOrganization: () => ({ organization: null, membership: null }),
}));

// Real useFocusEffect needs a navigation container; an effect is equivalent
// for a single mount.
jest.mock("expo-router", () => {
  const { useEffect } = jest.requireActual("react");
  return { useFocusEffect: (cb: () => void) => useEffect(cb, [cb]) };
});

jest.mock("../../../lib/pushDevice", () => ({ unregisterDevice: jest.fn() }));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  createApiClient: () => ({
    get: (path: string) =>
      path === "/billing/account"
        ? Promise.resolve(mockAccountFixture)
        : Promise.reject(new Error("unavailable")),
    del: () => Promise.resolve(),
  }),
}));

import Settings from "../settings";

describe("Settings plan card", () => {
  it("renders the free tier as Free plan (never the raw trial_local status)", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "trial_local",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Isol8 Free plan")).toBeTruthy();
    expect(screen.getByText(/daily free usage allowance/i)).toBeTruthy();
    expect(screen.getByText("Upgrade on isol8.co")).toBeTruthy();
    // Provider card: free tier runs Claude Sonnet — not "No provider selected".
    expect(screen.getByText("Claude Sonnet")).toBeTruthy();
    expect(screen.getByText("Included with the free plan")).toBeTruthy();
    expect(screen.queryByText(/trial_local/i)).toBeNull();
    expect(screen.queryByText("Isol8 — $50 / month")).toBeNull();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
  });

  it("renders a paid account unchanged", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "chatgpt_oauth",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Isol8 — $50 / month")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("Manage billing on isol8.co")).toBeTruthy();
  });
});
