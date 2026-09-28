import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette, colors } from "../../../config/theme";
import type { BoardIssue } from "../../../lib/teamsData";
import { IssueCard } from "../IssueCard";

// Codex review (PR #3341): dark scheme reused the LIGHT `colors.status`
// accents (and the server's raw label hex) as chip/label TEXT, which read
// ~2-3:1 against the dark card surface. See STATUS_DARK in teamsData.ts and
// the label-color fix in IssueCard.tsx.
jest.mock("react-native/Libraries/Utilities/useColorScheme", () => ({
  __esModule: true,
  default: () => "dark",
}));

function baseIssue(overrides: Partial<BoardIssue> = {}): BoardIssue {
  return {
    id: "issue-1",
    identifier: "ENG-1",
    title: "Fix the thing",
    status: "blocked",
    priority: null,
    labels: null,
    ...overrides,
  };
}

const noop = () => {};

describe("IssueCard dark-scheme contrast (Codex #3341)", () => {
  it("renders the status chip text in the dark-variant color, not the light accent", async () => {
    await render(
      <IssueCard
        issue={baseIssue({ status: "blocked" })}
        assignee={null}
        subtaskCount={0}
        onPress={noop}
        onMove={noop}
      />,
    );
    const label = screen.getByText("Blocked");
    const color = StyleSheet.flatten(label.props.style).color;
    expect(color).toBe(chatPalette.dark.alert);
    expect(color).not.toBe(colors.status.blocked.color);
  });

  it("gives the priority indicator a dark-legible accent, not the light one", async () => {
    await render(
      <IssueCard
        issue={baseIssue({ status: "blocked", priority: "critical" })}
        assignee={null}
        subtaskCount={0}
        onPress={noop}
        onMove={noop}
      />,
    );
    const [icon] = screen.root!.queryAll((node) => node.props.name === "exclamationmark.triangle");
    expect(icon).toBeTruthy();
    expect(icon.props.tintColor).toBe(chatPalette.dark.alert);
    expect(icon.props.tintColor).not.toBe(colors.priority.critical);
  });

  it("renders label text in a palette foreground, not the server's raw hex", async () => {
    await render(
      <IssueCard
        issue={baseIssue({
          labels: [{ id: "l1", name: "Backend", color: "#2F5D50" }],
        })}
        assignee={null}
        subtaskCount={0}
        onPress={noop}
        onMove={noop}
      />,
    );
    const label = screen.getByText("Backend");
    const color = StyleSheet.flatten(label.props.style).color;
    expect(color).toBe(chatPalette.dark.fg);
    expect(color).not.toBe("#2F5D50");
  });
});
