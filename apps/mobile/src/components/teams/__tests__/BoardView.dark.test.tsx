import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

// BoardView imports teamsData, which imports expo-router (useFocusEffect) at
// module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

jest.mock("react-native/Libraries/Utilities/useColorScheme", () => ({
  __esModule: true,
  default: () => "dark",
}));

let mockReadyState: unknown;
jest.mock("../../../lib/teamsData", () => ({
  ...jest.requireActual("../../../lib/teamsData"),
  useTeamsBoard: () => ({
    state: mockReadyState,
    refreshing: false,
    retry: jest.fn(),
    refresh: jest.fn(),
    moveIssue: jest.fn(),
  }),
}));

import { chatPalette, colors } from "../../../config/theme";
import { BoardView } from "../BoardView";

// Regression: SectionHeader used to call `statusColors(status)` with no
// scheme, so its dot always drew the LIGHT accent even in dark mode — a
// ~2-3:1 chip on a dark card (same class of bug IssueCard.test.tsx guards).
describe("BoardView dark-scheme section header", () => {
  it("colors the section dot from the dark palette, not the light `colors.status` accent", async () => {
    mockReadyState = {
      kind: "ready",
      issues: [
        {
          id: "issue-1",
          identifier: "ENG-1",
          title: "Fix the thing",
          status: "blocked",
          priority: null,
          labels: null,
        },
      ],
      agents: [],
      members: [],
    };
    await render(<BoardView />);
    await screen.findByText("BLOCKED");

    const hasBackground = (color: string) =>
      screen.root!.queryAll(
        (node) => StyleSheet.flatten(node.props.style)?.backgroundColor === color,
      ).length > 0;

    expect(hasBackground(chatPalette.dark.alert)).toBe(true);
    expect(hasBackground(colors.status.blocked.color)).toBe(false);
  });
});
