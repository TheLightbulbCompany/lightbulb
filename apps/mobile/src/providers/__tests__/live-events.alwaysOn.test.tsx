import { render, waitFor } from "@testing-library/react-native";
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
import { LiveEventsProvider } from "../live-events";

const LIVE_CONFIG = {
  schemaVersion: 1,
  httpEndpoint: "https://events.example/event",
  realtimeEndpoint: "wss://events.example/event/realtime",
  region: "us-east-1",
  userChannelKey: "k",
  workspaceId: "ws-a",
};

describe("LiveEventsProvider (mobile): the super-agent path is the only path", () => {
  it("reads the agents list though live config's features are null", async () => {
    mockGet.mockResolvedValue({ ...LIVE_CONFIG, features: null });
    await render(<LiveEventsProvider><Text>app</Text></LiveEventsProvider>);
    await waitFor(() => expect(new Set(mockGet.mock.calls.map(([path]) => path)))
      .toEqual(new Set(["/messaging/live-config", "/teams/agents"])));
  });

});
