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

function mockEndpoints() {
  mockGet.mockImplementation((endpoint: string) => {
    if (endpoint === "/billing/usage") return Promise.resolve(usageFixture);
    return Promise.reject(new Error(`unexpected endpoint ${endpoint}`));
  });
}

describe("UsagePanel usage display", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it("renders token tiles + request count, no dollar sign anywhere", async () => {
    mockEndpoints();

    await render(<UsagePanel />);

    expect(await screen.findByText("42")).toBeTruthy();
    expect(screen.getByText("12K")).toBeTruthy();
    expect(JSON.stringify(screen.toJSON())).not.toContain("$");
  });

  it("member rows: show name + request count, never a member dollar value", async () => {
    mockEndpoints();

    await render(<UsagePanel />);

    expect(await screen.findByText("Alice")).toBeTruthy();
    expect(screen.getByText("7 req")).toBeTruthy();
    expect(JSON.stringify(screen.toJSON())).not.toContain("$");
  });
});
