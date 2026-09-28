import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

// InteractionCard imports teamsData, which imports expo-router
// (useFocusEffect) at module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

jest.mock("react-native/Libraries/Utilities/useColorScheme", () => ({
  __esModule: true,
  default: () => "dark",
}));

import { chatPalette } from "../../../config/theme";
import type { IssueInteraction } from "../../../lib/teamsData";
import { InteractionCard } from "../InteractionCard";

const agents = [{ id: "agent_1", name: "Friday" }];

function makeInteraction(overrides: Partial<IssueInteraction>): IssueInteraction {
  return {
    id: "int_1",
    issueId: "iss_1",
    kind: "request_confirmation",
    status: "pending",
    createdByAgentId: "agent_1",
    createdAt: new Date().toISOString(),
    payload: { prompt: "Ship it?", detailsMarkdown: "Plain detail text" },
    ...overrides,
  } as IssueInteraction;
}

// Regression: `detailsMarkdown` used to render inside a hardcoded light
// "well" (`colors.creamDeeper` background, unthemed MessageMarkdown) sitting
// directly on the card's dark surface — unreadable. It now renders with the
// same dark palette as the rest of the card, no well.
describe("InteractionCard dark scheme", () => {
  it("renders the title and detail markdown in the dark palette", async () => {
    await render(
      <InteractionCard
        interaction={makeInteraction({})}
        agents={agents}
        onAccept={jest.fn()}
        onReject={jest.fn()}
        onRespond={jest.fn()}
      />,
    );

    const title = screen.getByText("Ship it?");
    expect(StyleSheet.flatten(title.props.style).color).toBe(chatPalette.dark.fg);

    const details = screen.getByText("Plain detail text");
    expect(StyleSheet.flatten(details.props.style).color).toBe(chatPalette.dark.fg);
  });
});
