import { render, screen } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub the useFocusEffect
// hook <UsagePanel/> uses so it fires its callback once on mount via a real
// effect (chat.test.tsx mocks the same module for `router`) — calling the
// callback synchronously during render would trigger setState-during-render
// and React's "too many re-renders" guard.
jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return { useFocusEffect: (cb: () => void) => React.useEffect(cb, [cb]) };
});

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
  useOrganization: () => ({ organization: null, membership: null }),
}));

const mockGet = jest.fn();

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet }),
}));

import UsagePanel from "../usage";

const usageFixture = {
  period: "2026-07",
  total_spend: 0,
  total_input_tokens: 12000,
  total_output_tokens: 4500,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  request_count: 42,
  lifetime_spend: 0,
  by_member: [
    {
      user_id: "u1",
      display_name: "Alice",
      email: null,
      total_spend: 0,
      total_input_tokens: 10,
      total_output_tokens: 20,
      request_count: 7,
    },
  ],
};

function mockEndpoints(spend: unknown) {
  mockGet.mockImplementation((endpoint: string) => {
    if (endpoint === "/billing/usage") return Promise.resolve(usageFixture);
    if (endpoint === "/billing/spend") return Promise.resolve(spend);
    return Promise.reject(new Error(`unexpected endpoint ${endpoint}`));
  });
}

describe("UsagePanel provider-aware spend", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it("tokens_only: renders token tiles + request count, no dollar sign anywhere", async () => {
    mockEndpoints({
      mode: "tokens_only",
      billed_month_to_date_cents: null,
      by_model: null,
      period_start: null,
      plan: null,
      windows: null,
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("42")).toBeTruthy();
    expect(screen.getByText("12K")).toBeTruthy();
    expect(JSON.stringify(screen.toJSON())).not.toContain("$");
  });

  it("bedrock: renders Billed this month, dollar total, and per-model row", async () => {
    mockEndpoints({
      mode: "bedrock",
      billed_month_to_date_cents: 1234,
      by_model: [{ model: "Claude Opus", tier: "opus", cents: 900 }],
      period_start: "2026-07-01",
      plan: null,
      windows: null,
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("Billed this month")).toBeTruthy();
    expect(screen.getByText("$12.34")).toBeTruthy();
    expect(screen.getByText("Claude Opus")).toBeTruthy();
  });

  it("oauth_quota: renders per-window label + used percent", async () => {
    mockEndpoints({
      mode: "oauth_quota",
      billed_month_to_date_cents: null,
      by_model: null,
      period_start: null,
      plan: null,
      windows: [{ label: "Weekly", used_percent: 61.4, resets_at_ms: null }],
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("Weekly limit")).toBeTruthy();
    expect(screen.getByText("61% used")).toBeTruthy();
  });

  it("oauth_quota: renders plan badge as-is and reset time when present", async () => {
    mockEndpoints({
      mode: "oauth_quota",
      billed_month_to_date_cents: null,
      by_model: null,
      period_start: null,
      plan: "Pro Lite",
      windows: [{ label: "Weekly", used_percent: 61.4, resets_at_ms: 1234567890000 }],
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("Pro Lite")).toBeTruthy();
    expect(screen.getByText(/61% used · resets/)).toBeTruthy();
  });

  it("oauth_quota: shows <1% used for a fractional used_percent instead of rounding to 0%", async () => {
    mockEndpoints({
      mode: "oauth_quota",
      billed_month_to_date_cents: null,
      by_model: null,
      period_start: null,
      plan: "Free",
      windows: [{ label: "Daily", used_percent: 0.4, resets_at_ms: null }],
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("<1% used")).toBeTruthy();
  });

  it("member rows: show name + request count, never a member dollar value", async () => {
    mockEndpoints({
      mode: "tokens_only",
      billed_month_to_date_cents: null,
      by_model: null,
      period_start: null,
      plan: null,
      windows: null,
    });

    await render(<UsagePanel />);

    expect(await screen.findByText("Alice")).toBeTruthy();
    expect(screen.getByText("7 req")).toBeTruthy();
    expect(JSON.stringify(screen.toJSON())).not.toContain("$");
  });
});
