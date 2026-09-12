import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
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

import { MessageActionsSheet, QUICK_REACTIONS } from "../MessageActionsSheet";

describe("MessageActionsSheet", () => {
  beforeEach(() => mockSetStringAsync.mockClear());

  it("offers the fixed reaction set and reports the toggle direction", async () => {
    const onReact = jest.fn();
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onReact={onReact}
        onReplyInThread={() => {}}
        onMarkUnread={() => {}}
      />,
    );
    expect(QUICK_REACTIONS).toEqual(["👍", "✅", "🎉", "👀"]);
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    expect(onReact).toHaveBeenCalledWith("🎉", true);
    await fireEvent.press(screen.getByLabelText("Remove 👍 reaction"));
    expect(onReact).toHaveBeenCalledWith("👍", false);
  });

  it("copies the message text", async () => {
    const onClose = jest.fn();
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={onClose}
        onReact={() => {}}
        onReplyInThread={() => {}}
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
        onReact={() => {}}
        onReplyInThread={() => {}}
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

  it("raises reply-in-thread and mark-unread to the caller", async () => {
    const onReplyInThread = jest.fn();
    const onMarkUnread = jest.fn();
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onReact={() => {}}
        onReplyInThread={onReplyInThread}
        onMarkUnread={onMarkUnread}
      />,
    );
    await fireEvent.press(screen.getByText("Reply in thread"));
    expect(onReplyInThread).toHaveBeenCalledTimes(1);
    await fireEvent.press(screen.getByText("Mark unread"));
    expect(onMarkUnread).toHaveBeenCalledTimes(1);
  });

  // Both are the thread screen's shape, and both are the web's `!inThread` rule:
  // inside a thread there is nowhere to reply-in-thread TO (the composer below
  // already is that thread), and the read cursor is per room, so a thread cannot
  // express its own unread at all.
  it("omits Reply in thread and Mark unread when the caller passes no handler", async () => {
    await render(
      <MessageActionsSheet
        message={message}
        currentUserId="user-a"
        roomId="room-1"
        onClose={() => {}}
        onReact={() => {}}
      />,
    );
    expect(screen.queryByText("Reply in thread")).toBeNull();
    expect(screen.queryByText("Mark unread")).toBeNull();
    // The sheet is still open and still useful.
    expect(screen.getByText("Copy text")).toBeTruthy();
    expect(screen.getByText("Copy link")).toBeTruthy();
  });
});
