import { fireEvent, render, screen } from "@testing-library/react-native";
import * as Haptics from "expo-haptics";

import { EmojiPickerSheet } from "../EmojiPickerSheet";

// `recentsStore` inside the component is a module-level singleton (one
// recents list per device, by design -- see the component's doc comment), so
// a pick in one test is still there for the next one in this file. Tests
// below are ordered so nothing asserts an emoji is ABSENT from Recent after
// an earlier test may have picked it, and the two picks below use different
// emoji so neither's own `getByLabelText` turns ambiguous against the other's
// leftover Recent entry.
describe("EmojiPickerSheet", () => {
  beforeEach(() => jest.clearAllMocks());

  it("browses the categorized grid with no query", async () => {
    await render(<EmojiPickerSheet visible onClose={() => {}} onPick={() => {}} />);
    expect(screen.getByText("Smileys")).toBeTruthy();
    // A distinct, real label -- not the glyph -- per cell.
    expect(screen.getByLabelText("grinning face")).toBeTruthy();
  });

  it("filters to search results, dropping categories and recents", async () => {
    await render(<EmojiPickerSheet visible onClose={() => {}} onPick={() => {}} />);
    await fireEvent.changeText(screen.getByLabelText("Search emoji"), "rocket");
    expect(screen.getByLabelText("rocket")).toBeTruthy();
    expect(screen.queryByText("Smileys")).toBeNull();
  });

  it("reports no matches for a query nothing satisfies", async () => {
    await render(<EmojiPickerSheet visible onClose={() => {}} onPick={() => {}} />);
    await fireEvent.changeText(screen.getByLabelText("Search emoji"), "zzzznomatch");
    expect(screen.getByText("No matching emoji.")).toBeTruthy();
  });

  it("does not close itself on a pick -- that stays the caller's call via `visible`", async () => {
    const onClose = jest.fn();
    await render(<EmojiPickerSheet visible onClose={onClose} onPick={() => {}} />);
    await fireEvent.changeText(screen.getByLabelText("Search emoji"), "fire");
    await fireEvent.press(screen.getByLabelText("fire"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("hands a pick to the caller, gives selection haptics, and records it as Recent", async () => {
    const onPick = jest.fn();
    await render(<EmojiPickerSheet visible onClose={() => {}} onPick={onPick} />);
    await fireEvent.press(screen.getByLabelText("grinning face"));
    expect(onPick).toHaveBeenCalledWith("😀");
    expect(Haptics.selectionAsync).toHaveBeenCalledTimes(1);
    // Reflected on the SAME open sheet -- the grid re-renders off local state,
    // it does not require a close/reopen round trip through the on-disk store.
    expect(screen.getByText("Recent")).toBeTruthy();
  });
});
