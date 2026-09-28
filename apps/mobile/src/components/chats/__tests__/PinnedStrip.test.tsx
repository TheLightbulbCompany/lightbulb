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
  { key: "session:s1", kind: "session", session: busy, name: "Codex" },
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
  expect(screen.getByLabelText("Move CEO left").props.accessibilityState).toMatchObject({ disabled: true });
  await fireEvent.press(screen.getByText("Done"));
  expect(h.onReorder).toHaveBeenCalledWith(false);
});
