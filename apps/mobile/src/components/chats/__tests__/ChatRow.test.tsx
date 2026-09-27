import { fireEvent, render, screen, within } from "@testing-library/react-native";
import type { ReactElement, ReactNode } from "react";
import type { LocalSession, RoomSummary } from "@lightbulb/shared";

// expo-router's Link, reduced to its contract here: the trigger's press
// navigates to `href`, the preview is native-only, and the menu's actions are
// drawn as plain buttons (scoped per row by the Link's testID) so a test can
// press them. Copied verbatim from `(tabs)/__tests__/home.test.tsx`.
const mockNavigate = jest.fn();
jest.mock("expo-router", () => {
  const { cloneElement, createContext, use } = require("react");
  const { Pressable, Text, View: RNView } = require("react-native");
  const Href = createContext("");
  function Link({ href, children }: { href: string; children?: ReactNode }) {
    return (
      <Href value={href}>
        <RNView testID={`link-${href}`}>{children}</RNView>
      </Href>
    );
  }
  Link.Trigger = function Trigger({
    children,
  }: {
    children: ReactElement<{ onPress?: () => void }>;
  }) {
    const href = use(Href);
    return cloneElement(children, { onPress: () => mockNavigate(href) });
  };
  Link.Preview = function Preview() {
    return null;
  };
  Link.Menu = function Menu({ children }: { children?: ReactNode }) {
    return <RNView>{children}</RNView>;
  };
  Link.MenuAction = function MenuAction({
    children,
    onPress,
  }: {
    children?: ReactNode;
    onPress?: () => void;
  }) {
    return (
      <Pressable onPress={onPress}>
        <Text>{children}</Text>
      </Pressable>
    );
  };
  return { Link };
});
// The swipe container, flattened: both action panels drawn beside the row, the
// way they sit behind it on device, with the `close` a pressed action calls.
jest.mock("react-native-gesture-handler/ReanimatedSwipeable", () => {
  const { View: RNView } = require("react-native");
  const swipe = { close: () => {}, openLeft() {}, openRight() {}, reset() {} };
  const progress = { value: 0 };
  return {
    __esModule: true,
    default: ({
      children,
      renderLeftActions,
      renderRightActions,
    }: {
      children?: ReactNode;
      renderLeftActions?: (p: unknown, t: unknown, s: unknown) => ReactNode;
      renderRightActions?: (p: unknown, t: unknown, s: unknown) => ReactNode;
    }) => (
      <RNView>
        {renderLeftActions?.(progress, progress, swipe)}
        {children}
        {renderRightActions?.(progress, progress, swipe)}
      </RNView>
    ),
  };
});

import { chatPalette } from "../../../config/theme";
import { RoomChatRow, SessionChatRow } from "../ChatRow";

const p = chatPalette.light;
const dm: RoomSummary = {
  roomId: "dm1", name: null, kind: "dm", roomRevision: 1, lastMessageSeq: 4, lastMessageAt: "2026-09-26T09:12:00Z",
  unreadCount: 2, updatedAt: "x", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 2,
  lastMessage: { messageId: "m", seq: 4, authorId: "ceo", authorKind: "agent", authorName: "CEO", textPreview: "Prod deploy landed", hasAttachments: false, createdAt: "x", threadRootMessageId: null },
};
const ceo = { kind: "agent" as const, id: "ceo", displayName: "CEO" };

beforeEach(() => mockNavigate.mockClear());

describe("RoomChatRow", () => {
  it("shows the counterpart, preview and unread dot, and opens the room", async () => {
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} live={null} hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.getByText("CEO")).toBeTruthy();
    expect(screen.getByText("Prod deploy landed")).toBeTruthy();
    expect(screen.getByLabelText("Unread")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("CEO, 2 unread"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/room/dm1");
  });

  it("says working in the preview while the DM agent runs", async () => {
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring="running" live="working…" hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.getByText("working…")).toBeTruthy();
    expect(screen.getByLabelText("Working")).toBeTruthy();
  });

  it("says who is typing in a channel row", async () => {
    await render(<RoomChatRow room={{ ...dm, kind: "channel", name: "general" }} others={[ceo]} viewerId="me" ring={null} live="Ann is typing…" hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.getByText("Ann is typing…")).toBeTruthy();
  });

  it("hides the dot and shows a bell for a muted room", async () => {
    await render(<RoomChatRow room={{ ...dm, muted: true }} others={[ceo]} viewerId="me" ring={null} live={null} hasDraft={false} pinned={false} palette={p} onAction={jest.fn()} />);
    expect(screen.queryByLabelText("Unread")).toBeNull();
    expect(screen.getByLabelText("CEO, muted")).toBeTruthy();
  });

  it("offers Pin in the menu and Unpin once pinned", async () => {
    const onAction = jest.fn();
    const { rerender } = await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} live={null} hasDraft={false} pinned={false} palette={p} onAction={onAction} />);
    await fireEvent.press(within(screen.getByTestId("link-/(app)/room/dm1")).getByText("Pin"));
    expect(onAction).toHaveBeenCalledWith("pin");
    await rerender(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} live={null} hasDraft={false} pinned palette={p} onAction={onAction} />);
    expect(within(screen.getByTestId("link-/(app)/room/dm1")).getByText("Unpin")).toBeTruthy();
  });

  it("swipes to mark read and to mute", async () => {
    const onAction = jest.fn();
    await render(<RoomChatRow room={dm} others={[ceo]} viewerId="me" ring={null} live={null} hasDraft={false} pinned={false} palette={p} onAction={onAction} />);
    await fireEvent.press(screen.getByLabelText("Mark read", { includeHiddenElements: true }));
    await fireEvent.press(screen.getByLabelText("Mute conversation", { includeHiddenElements: true }));
    expect(onAction.mock.calls).toEqual([["markRead"], ["mute"]]);
  });
});

describe("SessionChatRow", () => {
  const session: LocalSession = {
    sessionId: "s1", deviceId: "d", humanUserId: "leo", provider: "claude", generation: "g", status: "busy",
    providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
    latestEventSeq: 3, title: "lightbulb", lastEventAt: "2026-09-26T09:38:00Z",
  };

  it("draws the vendor mark, a live ring and the owner's working line", async () => {
    await render(<SessionChatRow session={session} ownerName="Leo" own={false} unread palette={p} onAction={jest.fn()} />);
    expect(screen.getByTestId("session-tool-claude", { includeHiddenElements: true })).toBeTruthy();
    expect(screen.getByLabelText("Working")).toBeTruthy();
    expect(screen.getByText("Leo · working…")).toBeTruthy();
  });

  it("opens the session and archives from the swipe", async () => {
    const onAction = jest.fn();
    await render(<SessionChatRow session={{ ...session, status: "idle" }} ownerName="Leo" own unread={false} palette={p} onAction={onAction} />);
    await fireEvent.press(screen.getByLabelText("Claude · lightbulb, Idle"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/session/s1");
    await fireEvent.press(screen.getByLabelText("Archive session", { includeHiddenElements: true }));
    expect(onAction).toHaveBeenCalledWith("archive");
  });
});
