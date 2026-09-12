import { fireEvent, render, screen } from "@testing-library/react-native";
import type { CanonicalRoomMessage, MessageAttachment, RoomMember } from "@lightbulb/shared";

import { colors } from "../../../config/theme";
import { MessageGroup } from "../MessageGroup";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

const downloadAttachment = jest.fn(async () => new Blob(["fake-bytes"], { type: "image/png" }));

const photoAttachment: MessageAttachment = {
  id: "att-1",
  fileName: "sunset.png",
  contentType: "image/png",
  size: 2048,
};

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

describe("MessageGroup attachments", () => {
  it("opens the full-screen viewer when an attachment is tapped", async () => {
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open sunset.png"));
    expect(await screen.findByLabelText("Close")).toBeTruthy();
    expect(screen.getByText("sunset.png")).toBeTruthy();
  });

  it("does not open the viewer on a plain long-press, and still calls onLongPress", async () => {
    const onLongPress = jest.fn();
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        onLongPress={onLongPress}
        downloadAttachment={downloadAttachment}
      />,
    );
    // The author name is a plain Text node (unlike the markdown body), so this
    // long-press bubbles to the group's own Pressable without depending on the
    // markdown renderer's internal tree shape.
    await fireEvent(screen.getByText("Friday"), "longPress");
    expect(onLongPress).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Close")).toBeNull();
  });

  it("pages between several attachments with an n of m indicator", async () => {
    const second: MessageAttachment = { ...photoAttachment, id: "att-2", fileName: "beach.png" };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment, second] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open sunset.png"));
    expect(screen.getByText("1 of 2")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Next attachment"));
    expect(screen.getByText("2 of 2")).toBeTruthy();
    expect(screen.getByText("beach.png")).toBeTruthy();
  });

  it("falls back to a share card for a non-image, non-PDF attachment", async () => {
    const video: MessageAttachment = {
      ...photoAttachment,
      id: "att-3",
      fileName: "demo.mov",
      // Cast: MessageAttachment's contentType union is images + PDF only; the
      // viewer reads contentType at runtime so it can't actually mistype this
      // at runtime, only past the shared type.
      contentType: "video/quicktime" as MessageAttachment["contentType"],
    };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [video] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open demo.mov"));
    // The share-card action carries its own, differently-worded label so this
    // assertion can't accidentally match the still-mounted list row's tap target.
    expect(await screen.findByLabelText("Open demo.mov in another app")).toBeTruthy();
    expect(screen.getByText("2.0 KB · video/quicktime")).toBeTruthy();
  });

  it("renders a PDF attachment in the WebView page", async () => {
    const pdf: MessageAttachment = {
      ...photoAttachment,
      id: "att-4",
      fileName: "invoice.pdf",
      contentType: "application/pdf",
    };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [pdf] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open invoice.pdf"));
    expect(await screen.findByTestId("mock-webview")).toBeTruthy();
  });

  it("closes the viewer", async () => {
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment] }}
        collapsed={false}
        replyCount={0}
        currentUserId="user-a"
        downloadAttachment={downloadAttachment}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open sunset.png"));
    expect(screen.queryByLabelText("Close")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Close"));
    expect(screen.queryByLabelText("Close")).toBeNull();
  });
});
