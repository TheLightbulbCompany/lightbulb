import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Linking } from "react-native";
import type { CanonicalRoomMessage } from "@lightbulb/shared";

// `mock`-prefixed because babel-plugin-jest-hoist rejects any other out-of-scope
// name a factory reaches for, and referenced lazily for the same reason the rest
// of this branch's suites do: babel lowers the `const` to `var`.
const mockSetStringAsync = jest.fn(async (_text: string) => true);
jest.mock("expo-clipboard", () => ({ setStringAsync: (text: string) => mockSetStringAsync(text) }), {
  virtual: true,
});

const message: CanonicalRoomMessage = {
  messageId: "m1",
  roomId: "room-1",
  messageSeq: 2,
  roomRevision: 1,
  author: { kind: "agent", id: "agent-1", displayName: "Friday" },
  text: "ship it",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-11T10:00:00Z",
  runId: null,
  reactions: { "👍": ["user-a"] },
};

import { MessageActionsSheet, reportMessageUrl } from "../MessageActionsSheet";

describe("MessageActionsSheet", () => {
  beforeEach(() => mockSetStringAsync.mockClear());

  it("copies the message text", async () => {
    const onClose = jest.fn();
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={onClose}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Copy text"));
    await waitFor(() => expect(mockSetStringAsync).toHaveBeenCalledWith("ship it"));
    expect(onClose).toHaveBeenCalled();
  });

  it("copies a workspace permalink, not a message body", async () => {
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Copy link"));
    await waitFor(() =>
      expect(mockSetStringAsync).toHaveBeenCalledWith(
        expect.stringContaining("panel=chat&room=room-1&message=m1"),
      ),
    );
  });

  // App Store 1.2: report objectionable content by prefilled email.
  it("reports someone else's message to support by email", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Report"));
    const url = openURL.mock.calls[0][0];
    expect(url.startsWith("mailto:support@thelightbulb.company?")).toBe(true);
    const body = decodeURIComponent(url.split("&body=")[1]);
    expect(body).toContain("Room: room-1");
    expect(body).toContain("Message: m1");
    expect(body).toContain("Author: Friday (agent agent-1)");
    expect(body).toContain("Excerpt: ship it");
    openURL.mockRestore();
  });

  // A UTF-16 slice at 280 can split an emoji's surrogate pair, and
  // encodeURIComponent throws on the lone half, before openURL's .catch.
  it("builds the report link when the cut lands inside an emoji", () => {
    const text = `${"a".repeat(279)}😀 and more`;
    const url = reportMessageUrl("room-1", { ...message, text });
    const body = decodeURIComponent(url.split("&body=")[1]);
    expect(body).toContain(`Excerpt: ${"a".repeat(279)}😀…`);
  });

  it("does not offer Report on your own message", async () => {
    await render(
      <MessageActionsSheet
        message={{ ...message, author: { kind: "human", id: "user-a", displayName: "Me" } }}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText("Report")).toBeNull();
  });

  it("raises mark-unread to the caller", async () => {
    const onMarkUnread = jest.fn();
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onMarkUnread={onMarkUnread}
      />,
    );
    await fireEvent.press(screen.getByText("Mark unread"));
    expect(onMarkUnread).toHaveBeenCalledTimes(1);
  });

  // A tombstone as the backend projects one: `canonical_message` blanks text,
  // so copying it is a dead end. The web hides it on `deletedAt`.
  it("offers no Copy text on a deleted message", async () => {
    await render(
      <MessageActionsSheet
        message={{ ...message, deletedAt: "2026-09-11T10:05:00Z", text: "", reactions: {} }}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onMarkUnread={() => {}}
      />,
    );
    expect(screen.queryByText("Copy text")).toBeNull();
    // The permalink still resolves a tombstone, so this row stays.
    expect(screen.getByText("Copy link")).toBeTruthy();
    expect(screen.getByText("Mark unread")).toBeTruthy();
  });

  it("omits Mark unread when the caller passes no handler", async () => {
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText("Mark unread")).toBeNull();
    // The sheet is still open and still useful.
    expect(screen.getByText("Copy text")).toBeTruthy();
    expect(screen.getByText("Copy link")).toBeTruthy();
  });

  // The room screens now keep this component mounted and toggle `visible`
  // instead of conditionally rendering it (`{target && <.../>}`) — that old
  // shape gave BottomSheet no chance to animate out. This proves `visible`
  // is forwarded, not hardcoded, so the SAME instance can go hidden then
  // visible again via nothing but a prop change.
  it("stays mounted and honors visible toggles instead of a hardcoded open state", async () => {
    const onClose = jest.fn();
    const { rerender } = await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        visible={false}
        onClose={onClose}
      />,
    );
    expect(screen.queryByText("Copy text")).toBeNull();

    await rerender(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        visible
        onClose={onClose}
      />,
    );
    expect(screen.getByText("Copy text")).toBeTruthy();
  });
});
