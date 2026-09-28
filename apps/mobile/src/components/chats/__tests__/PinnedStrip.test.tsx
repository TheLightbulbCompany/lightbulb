import { fireEvent, render, screen } from "@testing-library/react-native";
import { ActionSheetIOS } from "react-native";
import type { LocalSession, RoomSummary } from "@lightbulb/shared";

import { chatPalette } from "../../../config/theme";
import type { PinnedItem } from "../../../lib/chats";
import { PinnedStrip } from "../PinnedStrip";

const p = chatPalette.light;
const general = { roomId: "gen", name: "general", kind: "channel", members: [] } as unknown as RoomSummary;
const busy = { sessionId: "s1", provider: "codex", status: "busy" } as unknown as LocalSession;
const items: PinnedItem[] = [
  { key: "session:s1", kind: "session", session: busy, name: "Codex", own: true },
  { key: "agent:ceo", kind: "agent", agentId: "ceo", name: "CEO", ring: "running", roomId: "dm1" },
  { key: "room:gen", kind: "room", room: general, name: "general", others: [] },
];
const handlers = () => ({ onOpen: jest.fn(), onUnpin: jest.fn(), onMove: jest.fn(), onReorder: jest.fn() });

afterEach(() => jest.restoreAllMocks());

it("renders nothing without items", async () => {
  await render(<PinnedStrip items={[]} palette={p} reordering={false} {...handlers()} />);
  expect(screen.queryByTestId("pinned-strip")).toBeNull();
});

it("opens a pin on tap and labels its ring", async () => {
  const h = handlers();
  await render(<PinnedStrip items={items} palette={p} reordering={false} {...h} />);
  await fireEvent.press(screen.getByLabelText("CEO, working"));
  expect(h.onOpen).toHaveBeenCalledWith(items[1]);
  expect(screen.getByTestId("session-tool-codex", { includeHiddenElements: true })).toBeTruthy();
});

// Every session wears the same logo, so its title is what tells it apart: drawn
// in full, with what it is doing under it.
it("draws a live session as a card with its title and state", async () => {
  const h = handlers();
  const session = { sessionId: "s2", provider: "claude", status: "busy" } as unknown as LocalSession;
  const card: PinnedItem = { key: "session:s2", kind: "session", session, name: "Container cleanup and agent setup", own: true };
  const waiting = { sessionId: "s3", provider: "codex", status: "waiting-approval" } as unknown as LocalSession;
  const asking: PinnedItem = { key: "session:s3", kind: "session", name: "Release notes", session: waiting, own: true };
  // Only its owner can answer a session: a teammate's is not waiting on the reader.
  const theirs: PinnedItem = { key: "session:s4", kind: "session", name: "Billing", session: { ...waiting, sessionId: "s4" }, own: false };
  await render(<PinnedStrip items={[card, asking, theirs]} palette={p} reordering={false} {...h} />);
  expect(screen.getByText("Container cleanup and agent setup")).toBeTruthy();
  expect(screen.getByText("working…")).toBeTruthy();
  expect(screen.getAllByText("needs you")).toHaveLength(1);
  expect(screen.getByText("waiting for input")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Claude · Container cleanup and agent setup, working"));
  expect(h.onOpen).toHaveBeenCalledWith(card);
  // A session is not a pin: nothing to unpin or reorder.
  expect(screen.getByLabelText("Codex · Release notes, needs attention").props.accessibilityActions).toBeUndefined();
});

it("offers Unpin and Reorder on long press", async () => {
  const h = handlers();
  const sheet = jest.spyOn(ActionSheetIOS, "showActionSheetWithOptions").mockImplementation((_o, cb) => cb(0));
  await render(<PinnedStrip items={items} palette={p} reordering={false} {...h} />);
  await fireEvent(screen.getByLabelText("general"), "longPress");
  expect(h.onUnpin).toHaveBeenCalledWith("room:gen");
  sheet.mockImplementation((_o, cb) => cb(1));
  await fireEvent(screen.getByLabelText("general"), "longPress");
  expect(h.onReorder).toHaveBeenCalledWith(true);
  sheet.mockRestore();
});

it("moves pins with buttons in reorder mode and hides sessions", async () => {
  const h = handlers();
  await render(<PinnedStrip items={items} palette={p} reordering {...h} />);
  expect(screen.queryByTestId("session-tool-codex", { includeHiddenElements: true })).toBeNull();
  await fireEvent.press(screen.getByLabelText("Move general left"));
  expect(h.onMove).toHaveBeenCalledWith("room:gen", -1);
  // Names stay while reordering: a tile shows one letter, and two agents can share it.
  expect(screen.getByText("CEO")).toBeTruthy();
  expect(screen.getByText("general")).toBeTruthy();
  expect(screen.getByLabelText("Move CEO left").props.accessibilityState).toMatchObject({ disabled: true });
  await fireEvent.press(screen.getByText("Done"));
  expect(h.onReorder).toHaveBeenCalledWith(false);
});
