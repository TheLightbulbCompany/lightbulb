import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { AssigneeAvatar } from "../AssigneeAvatar";

it("draws an agent as a tinted rounded square, never a circle", async () => {
  await render(<AssigneeAvatar assignee={{ name: "Growth", kind: "agent" }} size={28} />);
  const glyph = screen.getByText("G");
  // The tree depth between the glyph Text and the tile View isn't stable
  // across RN Testing Library versions, so the tile carries its own testID.
  const tile = screen.getByTestId("assignee-avatar");
  const style = StyleSheet.flatten(tile.props.style);
  expect(style.borderRadius).toBe(8);
  expect(style.backgroundColor).toBe(chatPalette.light.agentTint);
  expect(StyleSheet.flatten(glyph.props.style).color).toBe(chatPalette.light.onAgentTint);
});

it("keeps small footer avatars square too", async () => {
  await render(<AssigneeAvatar assignee={{ name: "Dana Lee", kind: "user" }} />);
  expect(screen.getByText("DL")).toBeTruthy();
  const tile = screen.getByTestId("assignee-avatar");
  expect(StyleSheet.flatten(tile.props.style).borderRadius).toBe(4);
});
