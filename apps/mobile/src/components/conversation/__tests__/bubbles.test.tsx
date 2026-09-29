import { fireEvent, render, screen } from "@testing-library/react-native";
import { Pressable, StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { Bubble, BubbleMeta, Chip, MetaText, WorkingBubble } from "../bubbles";

const p = chatPalette.light;

it("fills mine with ink on the right at 78%", async () => {
  await render(<Bubble mine palette={p} accessibilityLabel="You: hi" testID="b"><Text>hi</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style)).toMatchObject({
    backgroundColor: p.fg, alignSelf: "flex-end", maxWidth: "78%", borderRadius: 18,
  });
});

it("draws others as a stroked pane bubble on the left at 94%", async () => {
  await render(<Bubble mine={false} palette={p} accessibilityLabel="CEO: hi" testID="b"><Text>hi</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style)).toMatchObject({
    backgroundColor: p.surface, borderColor: p.edge, borderWidth: 1, alignSelf: "flex-start", maxWidth: "94%",
  });
});

it("lets long message content shrink inside the bubble width", async () => {
  await render(
    <Bubble mine palette={p} accessibilityLabel="You: long message" testID="b">
      <Text>{"unbroken".repeat(40)}</Text>
    </Bubble>,
  );
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style)).toMatchObject({
    flexShrink: 1,
    minWidth: 0,
  });
});

it("strokes a bubble that mentions me in link blue", async () => {
  await render(<Bubble mine={false} mentionsMe palette={p} accessibilityLabel="x" testID="b"><Text>x</Text></Bubble>);
  expect(StyleSheet.flatten(screen.getByTestId("b").props.style).borderColor).toBe(p.link);
});

it("never claims the press itself, so the message row's long press gets it", async () => {
  const onLongPress = jest.fn();
  await render(
    <Pressable onLongPress={onLongPress} testID="row">
      <Bubble mine={false} palette={p} accessibilityLabel="CEO: hi"><Text>hi</Text></Bubble>
    </Pressable>,
  );
  const bubble = screen.getByLabelText("CEO: hi");
  expect(bubble.props.onLongPress).toBeUndefined();
  expect(bubble.props.onResponderGrant).toBeUndefined();
  await fireEvent(bubble, "longPress");
  expect(onLongPress).toHaveBeenCalled();
});

it("makes an expandable chip a button that reports its state", async () => {
  const onPress = jest.fn();
  await render(<Chip label="Ran 5 tools" palette={p} onPress={onPress} expanded={false} />);
  const chip = screen.getByLabelText("Ran 5 tools");
  expect(chip.props.accessibilityState).toMatchObject({ expanded: false });
  await fireEvent.press(chip);
  expect(onPress).toHaveBeenCalled();
});

it("hangs meta text under a bubble", async () => {
  await render(<BubbleMeta align="end" palette={p}><MetaText palette={p}>Read</MetaText></BubbleMeta>);
  expect(StyleSheet.flatten(screen.getByText("Read").props.style)).toMatchObject({ fontSize: 12, color: p.fgMute });
});

it("shows a working bubble with its caption, announced politely", async () => {
  await render(<WorkingBubble palette={p} caption="Browsing example.com" accessibilityLabel="CEO is working" />);
  expect(screen.getByText("Browsing example.com")).toBeTruthy();
  expect(screen.getByLabelText("CEO is working").props.accessibilityLiveRegion).toBe("polite");
});
