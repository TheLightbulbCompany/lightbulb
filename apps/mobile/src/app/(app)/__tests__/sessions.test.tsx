import { render, screen, waitFor } from "@testing-library/react-native";
import type { LiveEvent } from "@lightbulb/shared";

jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: jest.fn() },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
  };
});
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));

const mockListWorkspaceSessions = jest.fn(async () => ({ sessions: [] }));
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: [], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [] })),
    listWorkspaceSessions: mockListWorkspaceSessions,
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (_handler: (event: LiveEvent) => void) => () => {},
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import SharedSessionsScreen from "../sessions";

describe("shared sessions screen", () => {
  it("says so when no one is sharing a terminal", async () => {
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("No one is sharing a terminal right now.")).toBeTruthy());
  });
});
