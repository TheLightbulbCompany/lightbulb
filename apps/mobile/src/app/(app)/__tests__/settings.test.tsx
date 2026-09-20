import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// Per-test billing fixture the mocked api client serves (factory is lazy —
// the closure reads this at request time, after each test assigns it). An
// Error is served as a failed request.
let mockAccountFixture: Record<string, unknown> | Error;
const mockPost = jest.fn(async () => ({ status: "deleted" }));

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

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: (path: string) =>
      path !== "/billing/account"
        ? Promise.reject(new Error("unavailable"))
        : mockAccountFixture instanceof Error
          ? Promise.reject(mockAccountFixture)
          : Promise.resolve(mockAccountFixture),
    del: () => Promise.resolve(),
    post: mockPost,
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
    expect(await screen.findByText("Lightbulb Free plan")).toBeTruthy();
    expect(screen.getByText(/30 days free. No credit card required/i)).toBeTruthy();
    // Provider card: free tier runs Claude Sonnet — not "No provider selected".
    expect(screen.getByText("Claude Sonnet")).toBeTruthy();
    expect(screen.getByText("Included with the free plan")).toBeTruthy();
    expect(screen.queryByText(/trial_local/i)).toBeNull();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
  });

  it("says when billing fails to load, and Retry asks again", async () => {
    mockAccountFixture = new Error("offline");
    await render(<Settings />);
    expect(await screen.findByText("Couldn’t load billing: offline")).toBeTruthy();

    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await fireEvent.press(screen.getByText("Retry"));
    expect(await screen.findByText("Lightbulb")).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load billing/)).toBeNull();
  });

  it("renders a paid account", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: "chatgpt_oauth",
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Lightbulb")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
  });

  // App Store 3.1.3(f): the exemption holds only while the app carries no
  // call to action for purchase outside it. This screen used to have two —
  // "Upgrade on flickmeon.ai" and "Manage billing on flickmeon.ai" — plus the plan
  // price. Plan STATE still renders; nothing that points at a purchase does.
  it.each([
    ["trial_local" as const],
    ["active" as const],
    [null],
  ])("carries no purchase call to action for a %s account", async (status) => {
    mockAccountFixture = {
      is_subscribed: status === "active",
      subscription_status: status,
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    await screen.findByText(/Lightbulb|No plan on this account/);

    expect(screen.queryByText(/upgrade/i)).toBeNull();
    expect(screen.queryByText(/manage billing/i)).toBeNull();
    expect(screen.queryByText(/\$50/)).toBeNull();
    // The legal links are the ONLY flickmeon.ai references that may remain.
    expect(screen.queryByText(/isol8\.co\/settings/)).toBeNull();
  });
});

describe("Settings account controls (App Store readiness)", () => {
  it("renders in-app legal links (5.1.1(i))", async () => {
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    expect(await screen.findByText("Privacy Policy")).toBeTruthy();
    expect(screen.getByText("Terms of Service")).toBeTruthy();
    expect(screen.getByText("Delete account")).toBeTruthy();
  });

  it("walks the two-step delete confirm and posts the typed email (5.1.1(v))", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    const promptSpy = jest.spyOn(Alert, "prompt").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      trial_end: null,
      provider_choice: null,
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    // Step 1: permanence warning.
    const [title, , buttons] = alertSpy.mock.calls[0] as [
      string,
      string,
      { text: string; onPress?: () => void }[],
    ];
    expect(title).toBe("Delete account");
    buttons.find((b) => b.text === "Continue")?.onPress?.();

    // Step 2: typed-email prompt → posts to the self-serve endpoint.
    const promptButtons = promptSpy.mock.calls[0][2] as {
      text: string;
      onPress?: (v?: string) => void;
    }[];
    promptButtons.find((b) => b.text === "Delete forever")?.onPress?.("me@x.co");
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/users/me/delete", {
        confirm_email: "me@x.co",
      }),
    );
    alertSpy.mockRestore();
    promptSpy.mockRestore();
  });
});

// An Apple subscription OUTLIVES the account: wipe_account deletes the Stripe
// customer, the billing row and the Clerk user, but cannot touch StoreKit.
// Delete without cancelling and Apple keeps charging for something that now
// entitles nothing — and the appAccountToken binding means the purchase cannot
// be restored onto a replacement account either.
describe("deleting an Apple-billed account", () => {
  it("warns and offers iOS Settings before the irreversible step", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "apple_iap",
      trial_end: null,
      provider_choice: "bedrock_claude",
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    const [title, body, buttons] = alertSpy.mock.calls[0] as [
      string,
      string,
      { text: string }[],
    ];
    expect(title).toMatch(/cancel your subscription first/i);
    expect(body).toMatch(/does not cancel/i);
    expect(buttons.map((b) => b.text)).toEqual(["Not now", "Open iOS Settings", "Delete anyway"]);
    alertSpy.mockRestore();
  });

  it("goes straight to the normal confirmation for a Stripe subscriber", async () => {
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    mockAccountFixture = {
      is_subscribed: true,
      subscription_status: "active",
      subscription_source: "stripe",
      trial_end: null,
      provider_choice: "bedrock_claude",
      byo_provider: null,
    };
    await render(<Settings />);
    fireEvent.press(await screen.findByText("Delete account"));

    expect(alertSpy.mock.calls[0][0]).toBe("Delete account");
    alertSpy.mockRestore();
  });
});
