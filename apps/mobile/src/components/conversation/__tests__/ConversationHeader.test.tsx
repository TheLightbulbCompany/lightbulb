import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { ConversationHeader } from "../ConversationHeader";

const p = chatPalette.light;

it("names the conversation and its status, and opens details", async () => {
  const onPress = jest.fn();
  await render(<ConversationHeader title="CEO" status="working…" statusTone="live" avatar={<Text>A</Text>} palette={p} onPress={onPress} />);
  await fireEvent.press(screen.getByLabelText("CEO, working…"));
  expect(onPress).toHaveBeenCalled();
  expect(StyleSheet.flatten(screen.getByText("working…").props.style).color).toBe(p.live);
});

it("omits an empty status", async () => {
  await render(<ConversationHeader title="Leo" avatar={null} palette={p} />);
  expect(screen.getByLabelText("Leo")).toBeTruthy();
});

it("exposes no button semantics when onPress is absent", async () => {
  await render(<ConversationHeader title="Thread" avatar={null} palette={p} />);
  const el = screen.getByLabelText("Thread");
  expect(el.props.accessibilityRole).toBeUndefined();
  expect(el.props.accessibilityHint).toBeUndefined();
});
