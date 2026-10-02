import { render, screen } from "@testing-library/react-native";

// App Store 3.1.1/3.1.3(f): a consumable-credits balance may be SHOWN, but
// nothing here may point at buying more — the "Top up on thelightbulb.company" link
// that used to sit under the balance is gone.

let mockAccountFixture: Record<string, unknown> | Error;

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
  useOrganization: () => ({ organization: null, membership: null }),
}));

jest.mock("expo-router", () => {
  const { useEffect } = jest.requireActual("react");
  return { useFocusEffect: (cb: () => void) => useEffect(cb, [cb]) };
});

jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({}),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: (path: string) => {
      if (path === "/billing/account") {
        return mockAccountFixture instanceof Error
          ? Promise.reject(mockAccountFixture)
          : Promise.resolve(mockAccountFixture);
      }
      if (path === "/billing/credits/balance") {
        return Promise.resolve({ balance_microcents: 12_340_000, balance_dollars: "12.34" });
      }
      return Promise.reject(new Error(`unexpected path: ${path}`));
    },
  }),
  useStatusLoaded: () => true,
  useRuntimeRpc: () => ({ data: undefined }),
}));

import LlmPanel from "../llm";

describe("LlmPanel workspace credits", () => {
  it("shows the balance with no top-up call to action", async () => {
    mockAccountFixture = { provider_choice: "openrouter_managed", byo_provider: null };
    await render(<LlmPanel />);

    expect(await screen.findByText("$12.34")).toBeTruthy();
    expect(screen.queryByText(/top up/i)).toBeNull();
    // The provider deep-link is a plain feature link, not a purchase CTA —
    // unaffected by the 3.1.3(f) fix and expected to remain.
    expect(screen.getByText("Manage provider on thelightbulb.company")).toBeTruthy();
  });

  it("shows the used daily allowance with no Upgrade link", async () => {
    mockAccountFixture = { provider_choice: null, byo_provider: null, capped_today: true };
    await render(<LlmPanel />);

    expect(await screen.findByText("Daily allowance used — resets at 00:00 UTC")).toBeTruthy();
    expect(screen.queryByText("Upgrade")).toBeNull();
  });

  it("labels a free-trial owner's null provider as managed, not unset", async () => {
    mockAccountFixture = {
      provider_choice: null,
      byo_provider: null,
      subscription_status: "trial_local",
    };
    await render(<LlmPanel />);

    expect(await screen.findByText("Lightbulb-managed models")).toBeTruthy();
    expect(screen.getByText("DeepSeek within a daily allowance")).toBeTruthy();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
  });

  it("labels a paid owner who never picked a provider as managed, not unset", async () => {
    mockAccountFixture = { provider_choice: null, byo_provider: null, plan: "standard" };
    await render(<LlmPanel />);

    expect(await screen.findByText("Lightbulb-managed models")).toBeTruthy();
    expect(screen.getByText("DeepSeek within a daily allowance")).toBeTruthy();
    expect(screen.queryByText(/No provider selected/)).toBeNull();
  });

  it("keeps the capped-today allowance line alongside the managed label", async () => {
    mockAccountFixture = {
      provider_choice: null,
      byo_provider: null,
      subscription_status: "trial_local",
      capped_today: true,
    };
    await render(<LlmPanel />);

    expect(await screen.findByText("Lightbulb-managed models")).toBeTruthy();
    expect(screen.getByText("Daily allowance used — resets at 00:00 UTC")).toBeTruthy();
    expect(screen.queryByText("Upgrade")).toBeNull();
  });
});
