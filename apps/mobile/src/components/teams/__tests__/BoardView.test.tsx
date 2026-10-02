import { render, screen } from "@testing-library/react-native";

// App Store 3.1.1/3.1.3(f): a workspace without an active subscription must
// see plain, neutral state for Teams — no "Manage on thelightbulb.company" button and
// no server-shaped subscription pitch.

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

let mockGateState: { kind: string; gate?: string; message?: string | null };
jest.mock("../../../lib/teamsData", () => ({
  ...jest.requireActual("../../../lib/teamsData"),
  useTeamsBoard: () => ({
    state: mockGateState,
    refreshing: false,
    retry: jest.fn(),
    refresh: jest.fn(),
    moveIssue: jest.fn(),
  }),
}));

import { BoardView } from "../BoardView";

describe("BoardView subscription gate", () => {
  it("shows neutral state with no outbound button", async () => {
    mockGateState = { kind: "gate", gate: "needs_subscription", message: null };
    await render(<BoardView />);

    expect(
      await screen.findByText("Teams isn't available for this workspace right now."),
    ).toBeTruthy();
    expect(screen.queryByText(/flickmeon\.ai/)).toBeNull();
    expect(screen.queryByText(/Manage/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps the Retry button for a non-purchase gate", async () => {
    mockGateState = { kind: "gate", gate: "unavailable", message: "offline" };
    await render(<BoardView />);

    expect(await screen.findByText("Retry")).toBeTruthy();
  });
});
