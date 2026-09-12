import { fireEvent, render, screen } from "@testing-library/react-native";
import type { CanonicalRoomMessage } from "@lightbulb/shared";

import { MessageGroup } from "../MessageGroup";

const downloadAttachment = jest.fn(async () => new Blob());

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
  reactions: { "👍": ["user-a", "user-b"], "🎉": [] },
};

describe("MessageGroup reactions", () => {
  it("renders an applied reaction chip with its count", async () => {
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        currentUserId="user-c"
        onToggleReaction={() => {}}
        downloadAttachment={downloadAttachment}
      />,
    );
    // 🎉 has zero reactors and is filtered out; only 👍 renders.
    expect(screen.getByLabelText("Add 👍 reaction, 2")).toBeTruthy();
    expect(screen.queryByLabelText(/🎉/)).toBeNull();
  });

  it("shows selected state for a reaction the viewer applied", async () => {
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        onToggleReaction={() => {}}
        downloadAttachment={downloadAttachment}
      />,
    );
    const chip = screen.getByLabelText("Remove 👍 reaction, 2");
    expect(chip.props.accessibilityState).toEqual({ selected: true });
  });

  it("toggles through the onToggleReaction callback", async () => {
    const onToggleReaction = jest.fn();
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        onToggleReaction={onToggleReaction}
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Remove 👍 reaction, 2"));
    expect(onToggleReaction).toHaveBeenCalledWith("👍", false);
  });

  it("hides reactions on a deleted message even if reaction data is present", async () => {
    await render(
      <MessageGroup
        message={{ ...message, deletedAt: "2026-09-11T10:05:00Z" }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        onToggleReaction={() => {}}
        downloadAttachment={downloadAttachment}
      />,
    );
    expect(screen.queryByLabelText("Message reactions")).toBeNull();
    expect(screen.queryByLabelText(/reaction/)).toBeNull();
  });

  it("shows the failure copy when a reaction toggle fails", async () => {
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        onToggleReaction={() => {}}
        reactionError
        downloadAttachment={downloadAttachment}
      />,
    );
    const alert = screen.getByText("Could not update reaction. Try again.");
    expect(alert.props.accessibilityRole).toBe("alert");
  });
});
