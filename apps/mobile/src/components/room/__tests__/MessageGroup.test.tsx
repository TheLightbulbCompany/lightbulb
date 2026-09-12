import { fireEvent, render, screen } from "@testing-library/react-native";
import type { CanonicalRoomMessage, RoomMember } from "@lightbulb/shared";

import { colors } from "../../../config/theme";
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

const members: RoomMember[] = [
  {
    kind: "agent",
    id: "agent-1",
    displayName: "Friday",
    mentionHandle: "friday",
    role: "member",
    listenMode: "active",
    joinedAt: "x",
    joinedSeq: 0,
  },
  {
    kind: "human",
    id: "user-a",
    displayName: "Aden Kim",
    mentionHandle: null,
    role: "owner",
    listenMode: null,
    joinedAt: "x",
    joinedSeq: 0,
  },
];

const baseMessage: CanonicalRoomMessage = {
  messageId: "m1",
  roomId: "room-1",
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "human", id: "user-b", displayName: "Someone Else" },
  text: "hey @Friday can you ship this",
  mentions: [{ kind: "agent", id: "agent-1" }],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-12T10:00:00Z",
  runId: null,
};

describe("MessageGroup mentions", () => {
  it("renders a mention of another member as a chip, and leaves the row untinted for a viewer it doesn't name", async () => {
    await render(
      <MessageGroup
        message={baseMessage}
        collapsed={false}
        replyCount={0}
        downloadAttachment={downloadAttachment}
        members={members}
        currentUserId="user-a"
      />,
    );
    const chip = screen.getByText("@Friday");
    expect(chip).toHaveStyle({ color: colors.info, backgroundColor: colors.mentionBg });
    expect(screen.getByTestId("message-group")).not.toHaveStyle({
      backgroundColor: colors.mentionRowBg,
    });
  });

  it("tints the whole row when the message mentions the viewer", async () => {
    const message: CanonicalRoomMessage = {
      ...baseMessage,
      text: "hey @Aden Kim can you take a look",
      mentions: [{ kind: "human", id: "user-a" }],
    };
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        downloadAttachment={downloadAttachment}
        members={members}
        currentUserId="user-a"
      />,
    );
    expect(screen.getByText("@Aden Kim")).toBeTruthy();
    expect(screen.getByTestId("message-group")).toHaveStyle({
      backgroundColor: colors.mentionRowBg,
    });
  });

  it("leaves plain text with no mentions untouched", async () => {
    const message: CanonicalRoomMessage = {
      ...baseMessage,
      text: "just chatting, no mentions here",
      mentions: [],
    };
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        downloadAttachment={downloadAttachment}
        members={members}
        currentUserId="user-a"
      />,
    );
    expect(screen.getByText("just chatting, no mentions here")).toBeTruthy();
    expect(screen.getByTestId("message-group")).not.toHaveStyle({
      backgroundColor: colors.mentionRowBg,
    });
  });

  // An `@` token that names nobody in this room must stay prose — not a
  // linkifyMentions bug (that grammar is proven in the shared package's own
  // tests), but the case that would actually catch a mistake in how
  // MessageGroup calls it: a wrong/empty `members` list, or a future change
  // that bypasses the call, would otherwise pass every other test here too.
  it("leaves an @ token that matches no member as plain prose", async () => {
    const message: CanonicalRoomMessage = {
      ...baseMessage,
      text: "email me @ noon, or ask @Nobody about it",
      mentions: [],
    };
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
        replyCount={0}
        downloadAttachment={downloadAttachment}
        members={members}
        currentUserId="user-a"
      />,
    );
    expect(screen.getByText("email me @ noon, or ask @Nobody about it")).toBeTruthy();
    expect(screen.queryByText("@Nobody")).toBeNull();
    expect(screen.getByTestId("message-group")).not.toHaveStyle({
      backgroundColor: colors.mentionRowBg,
    });
  });
});
