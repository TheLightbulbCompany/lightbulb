import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../../config/theme";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("../../../../lib/tabHeader", () => ({ useTabHeader: () => {} }));
const mockNoop = async () => {};
const mockDashboard = { state: { kind: "loading" }, refreshing: false, retry: mockNoop, refresh: mockNoop };
const mockInbox = {
  state: {
    kind: "ready",
    items: [{ id: "i1", title: "Ship v2", identifier: "LB-12", status: "in_progress", unread: true, updatedAt: new Date().toISOString() }],
    failedRuns: [], agents: [],
  },
  refreshing: false, retry: mockNoop, refresh: mockNoop,
  dismissedRunIds: new Set<string>(), dismissFailedRun: jest.fn(), retryFailedRun: jest.fn(),
};
jest.mock("../../../../lib/teamsData", () => ({
  ...jest.requireActual("../../../../lib/teamsData"),
  useTeamsDashboard: () => mockDashboard,
  useTeamsInbox: () => mockInbox,
  useTeamsApprovals: () => ({ state: { kind: "loading" }, refreshing: false, retry: mockNoop, refresh: mockNoop }),
  useCreateTeamIssue: () => jest.fn(),
}));

import Teams from "../teams";

it("paints the page on the ground", async () => {
  await render(<Teams />);
  expect(StyleSheet.flatten(screen.getByTestId("teams-host").props.style).backgroundColor).toBe(chatPalette.light.ground);
});

it("shows inbox items as inset rows with their unread dot and label", async () => {
  await render(<Teams />);
  await fireEvent.press(screen.getByRole("button", { name: "Inbox" }));
  expect(screen.getByLabelText(/^Ship v2\. LB-12/)).toBeTruthy();
  expect(screen.getByLabelText("Unread")).toBeTruthy();
});
