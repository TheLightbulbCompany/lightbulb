import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { chatPalette } from "../../../config/theme";
import { ListRow } from "../ListRow";

const p = chatPalette.light;

describe("ListRow", () => {
  it("shows title, time and a two-line preview", async () => {
    await render(
      <ListRow palette={p} leading={<Text>AV</Text>} title="CEO" time="9:12" preview="Prod deploy landed" accessibilityLabel="CEO" />,
    );
    expect(screen.getByText("CEO")).toBeTruthy();
    expect(screen.getByText("9:12")).toBeTruthy();
    expect(screen.getByText("Prod deploy landed").props.numberOfLines).toBe(2);
  });

  it("marks unread with a labelled dot and a semibold title", async () => {
    await render(<ListRow palette={p} leading={null} title="general" unread accessibilityLabel="general, unread" />);
    expect(screen.getByLabelText("Unread")).toBeTruthy();
    expect(StyleSheet.flatten(screen.getByText("general").props.style).fontFamily).toBe("SourceSans3_600SemiBold");
  });

  it("draws a live preview in the link colour", async () => {
    await render(<ListRow palette={p} leading={null} title="CEO" preview="working…" previewTone="live" accessibilityLabel="CEO" />);
    expect(StyleSheet.flatten(screen.getByText("working…").props.style).color).toBe(p.link);
  });

  it("forwards press and long press, and carries its label", async () => {
    const onPress = jest.fn();
    const onLongPress = jest.fn();
    await render(<ListRow palette={p} leading={null} title="x" onPress={onPress} onLongPress={onLongPress} accessibilityLabel="Open x" />);
    expect(screen.getByRole("button", { name: "Open x" })).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Open x"));
    await fireEvent(screen.getByLabelText("Open x"), "longPress");
    expect(onPress).toHaveBeenCalled();
    expect(onLongPress).toHaveBeenCalled();
  });

  it("renders inert with neither onPress nor onLongPress: no button role, no press feedback", async () => {
    await render(<ListRow palette={p} leading={null} title="x" accessibilityLabel="Resolved x" />);
    expect(screen.queryByRole("button", { name: "Resolved x" })).toBeNull();
    const row = screen.getByLabelText("Resolved x");
    expect(StyleSheet.flatten(row.props.style).backgroundColor).toBe(p.surface);
    // A plain View has no press handler to invoke; firing "press" is a no-op.
    await fireEvent.press(row);
  });

  it("tints a highlighted row with palette.selected, on top of the row's own paint", async () => {
    await render(<ListRow palette={p} leading={null} title="x" highlighted accessibilityLabel="Open x" />);
    expect(StyleSheet.flatten(screen.getByLabelText("Open x").props.style).backgroundColor).toBe(p.selected);
  });

  it("renders a trailing accessory", async () => {
    await render(<ListRow palette={p} leading={null} title="x" trailing={<Text>Review</Text>} accessibilityLabel="x" />);
    expect(screen.getByText("Review")).toBeTruthy();
  });

  it("adds a one-line detail under the preview", async () => {
    await render(
      <ListRow palette={p} leading={null} title="Growth" preview="needs your approval" detail="Pilot X auto-replies" accessibilityLabel="x" />,
    );
    expect(screen.getByText("Pilot X auto-replies").props.numberOfLines).toBe(1);
  });
});
