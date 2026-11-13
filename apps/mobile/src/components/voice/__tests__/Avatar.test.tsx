import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { Avatar } from "../Avatar";

it("fills an agent tile with the agent tint and draws the initial in its foreground", async () => {
  await render(<Avatar name="Growth" kind="agent" size={56} palette={chatPalette.dark} />);
  expect(StyleSheet.flatten(screen.getByText("G").props.style).color).toBe(chatPalette.dark.onAgentTint);
  expect(StyleSheet.flatten(screen.getByTestId("avatar-tile").props.style)).toMatchObject({ backgroundColor: chatPalette.dark.agentTint, borderRadius: 12 });
});

it("draws a human's initials on a rounded square", async () => {
  await render(<Avatar name="Ann Lee" kind="human" size={28} />);
  expect(screen.getByText("AL")).toBeTruthy();
  expect(StyleSheet.flatten(screen.getByTestId("avatar-tile").props.style).borderRadius).toBe(8);
});
