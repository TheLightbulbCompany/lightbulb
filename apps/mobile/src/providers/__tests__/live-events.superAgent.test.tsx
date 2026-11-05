import { render, screen, waitFor } from "@testing-library/react-native";
import { Text } from "react-native";

const mockGet = jest.fn();
const mockPost = jest.fn(async () => ({}));

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: async () => "t", isLoaded: true, userId: "user-a" }),
  useOrganization: () => ({ organization: { id: "org-a" } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "uuid" }));
jest.mock("../../lib/adapter", () => ({ makeAdapter: () => ({}) }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: (...a: unknown[]) => mockGet(...a), post: mockPost }),
  useLiveEvents: () => ({ liveConnected: false, liveChannels: [], lastError: null, connectionRevision: 0 }),
}));

// eslint-disable-next-line import/first
import { LiveEventsProvider, useSuperAgent } from "../live-events";

const LIVE_CONFIG = {
  schemaVersion: 1,
  httpEndpoint: "https://events.example/event",
  realtimeEndpoint: "wss://events.example/event/realtime",
  region: "us-east-1",
  userChannelKey: "k",
  workspaceId: "ws-a",
};

function SwitchProbe() {
  return <Text>switch:{useSuperAgent() ? "on" : "off"}</Text>;
}

describe("useSuperAgent (mobile)", () => {
  beforeEach(() => mockGet.mockReset());

  it.each([
    ["absent", undefined, "off"],
    ["null", null, "off"],
    ["false", { superAgent: false }, "off"],
    ["true", { superAgent: true }, "on"],
  ])("reads the switch from the live config it already fetches (features %s)", async (_name, features, shown) => {
    mockGet.mockResolvedValue({ ...LIVE_CONFIG, ...(features === undefined ? {} : { features }) });
    await render(<LiveEventsProvider><SwitchProbe /></LiveEventsProvider>);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText(`switch:${shown}`)).toBeTruthy());
    // No request of its own: every GET is the live-config fetch the provider already makes.
    expect(new Set(mockGet.mock.calls.map(([path]) => path))).toEqual(new Set(["/messaging/live-config"]));
  });

  it("is off outside a provider", async () => {
    await render(<SwitchProbe />);
    expect(screen.getByText("switch:off")).toBeTruthy();
  });
});
