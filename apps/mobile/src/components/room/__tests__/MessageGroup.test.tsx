import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { CanonicalRoomMessage, LocalSession, MessageAttachment, RoomMember } from "@lightbulb/shared";
import * as Haptics from "expo-haptics";
import { StyleSheet } from "react-native";

import { chatPalette, colors } from "../../../config/theme";
import { LocalMessageRows, MessageGroup } from "../MessageGroup";


const p = chatPalette.light; // jest's useColorScheme is "light"

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

const attachmentUrl = jest.fn(async () => ({ url: "https://s3.example/video.mp4?sig=x", expiresIn: 900 }));

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

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

it("renders a session mention as a reference card that opens the session", async () => {
  const session = {
    sessionId: "s1", deviceId: "mac", humanUserId: "user-a", provider: "claude", generation: "g1",
    status: "idle", providerVersion: "1", controlUnavailableReason: null, activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
    latestEventSeq: 0, cwdName: "lightbulb", title: "Launch check",
  } satisfies LocalSession;
  await render(<MessageGroup message={{ ...message, author: { kind: "human", id: "user-a", displayName: "You" }, text: "@claude-lightbulb-s1 look",
    sessionMentions: ["s1"], sessionTargetLabels: { s1: "Claude Code · Launch check · You" } }}
    collapsed={false} currentUserId="user-a" attachmentUrl={attachmentUrl}
    sessionCandidates={[{ session, handle: "claude-lightbulb-s1", collided: false }]} />);
  expect(screen.getByText("@Claude Code · Launch check · You")).toBeTruthy();
  expect(screen.getByText("Idle")).toBeTruthy();
  expect(screen.queryByText("Waiting for session")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Open session Claude · Launch check, Idle"));
  const { router } = jest.requireMock("expo-router");
  expect(router.push).toHaveBeenCalledWith({ pathname: "/(app)/session/[sessionId]", params: { sessionId: "s1" } });
});

it("keeps a reference openable by id when the session is not in the workspace list", async () => {
  // An ended session leaves the list after a week but still opens by id; the session
  // screen, not the card, decides whether access is really gone.
  await render(<MessageGroup message={{ ...message, via: "lightbulb_mcp", text: "Please check",
    sessionMentions: ["s1"], sessionTargetLabels: { s1: "Codex · Launch check · Aden Kim" } }}
    collapsed={false} currentUserId="user-a" attachmentUrl={attachmentUrl} />);
  expect(screen.getByText("Codex · Launch check")).toBeTruthy();
  expect(screen.queryByText("Unavailable")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Open session Codex · Launch check"));
  const { router } = jest.requireMock("expo-router");
  expect(router.push).toHaveBeenCalledWith({ pathname: "/(app)/session/[sessionId]", params: { sessionId: "s1" } });
});

describe("MessageGroup long press", () => {
  it("keeps the whole-message long-press action alongside selectable message text", async () => {
    const onLongPress = jest.fn();
    await render(
      <MessageGroup
        message={message}
        collapsed={false}
       
        currentUserId="user-c"
        onLongPress={onLongPress}
        attachmentUrl={attachmentUrl}
      />,
    );

    let textNode: ReturnType<typeof screen.getByText> | null = screen.getByText("ship it");
    while (textNode && textNode.props.selectable !== true) textNode = textNode.parent;
    expect(textNode?.props.selectable).toBe(true);
    fireEvent(screen.getByTestId("message-group"), "longPress");
    expect(onLongPress).toHaveBeenCalledTimes(1);
    expect(Haptics.impactAsync).toHaveBeenCalledTimes(1);
    (Haptics.impactAsync as jest.Mock).mockClear();
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
       
        attachmentUrl={attachmentUrl}
        members={members}
        currentUserId="user-a"
      />,
    );
    const chip = screen.getByText("@Friday");
    // The mention chip is now recolored by the bubble palette (Task 1's
    // `bubbleStyles.mention`), not the legacy light-only `colors` scale — it
    // no longer carries a background wash, since a chip fill inside an
    // already-tinted bubble reads as noise (and would be illegible on a dark
    // `fg` mine bubble). Same hex as `colors.info`, sourced from the palette.
    expect(chip).toHaveStyle({ color: p.link });
    expect(screen.getByTestId("message-group")).not.toHaveStyle({
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
       
        attachmentUrl={attachmentUrl}
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
       
        attachmentUrl={attachmentUrl}
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

it("strokes the bubble when the message mentions the viewer", async () => {
  await render(
    <MessageGroup message={{ ...message, mentions: [{ kind: "human", id: "user-a" }] }} collapsed={false}
      currentUserId="user-a" attachmentUrl={attachmentUrl} />,
  );
  expect(StyleSheet.flatten(screen.getByTestId("message-bubble").props.style).borderColor).toBe(p.link);
});

describe("MessageGroup bubbles", () => {
  const base = { collapsed: false, currentUserId: "user-a", attachmentUrl };

  it("draws the viewer's message as a mine bubble with its read state", async () => {
    const own = { ...message, author: { kind: "human" as const, id: "user-a", displayName: "Ann" } };
    await render(<MessageGroup {...base} message={own} readLabel="Read" />);
    expect(StyleSheet.flatten(screen.getByTestId("message-bubble").props.style).backgroundColor).toBe(p.fg);
    expect(screen.getByLabelText("You: ship it")).toBeTruthy();
    expect(screen.getByText("Read")).toBeTruthy();
    expect(screen.queryByText("Ann")).toBeNull();
  });

  it("labels an agent message sent on its own", async () => {
    await render(<MessageGroup {...base} message={{ ...message, runId: null }} />);
    expect(screen.getByText("on its own")).toBeTruthy();
  });

  it("does not label a room-turn reply", async () => {
    await render(<MessageGroup {...base} message={{ ...message, runId: "run-1" }} />);
    expect(screen.queryByText("on its own")).toBeNull();
  });

  it("drops avatar and name on a continued run and hides the time until the run ends", async () => {
    await render(<MessageGroup {...base} message={message} collapsed showTime={false} />);
    expect(screen.queryByText("Friday")).toBeNull();
    expect(screen.queryByText(/\d:\d\d/)).toBeNull();
  });

  it("never carries a read state on a deleted message", async () => {
    await render(<MessageGroup {...base} message={{ ...message, deletedAt: "2026-09-26T00:00:00Z" }} readLabel="Read" />);
    expect(screen.getByText("Message deleted")).toBeTruthy();
    expect(screen.queryByText("Read")).toBeNull();
  });
});

describe("LocalMessageRows", () => {
  it("dims an in-flight row as Sending… and offers retry on a failed one", async () => {
    const onRetry = jest.fn();
    const own = { ...message, messageId: "local-1", messageSeq: 0, author: { kind: "human" as const, id: "user-a", displayName: "Ann" } };
    await render(
      <LocalMessageRows rows={[{ message: own, unsent: false }, { message: { ...own, messageId: "local-2" }, unsent: true }]}
        onRetry={onRetry} attachmentUrl={attachmentUrl} currentUserId="user-a" />,
    );
    expect(screen.getByText("Sending…")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Not sent, tap to retry"));
    expect(onRetry).toHaveBeenCalledWith("local-2");
  });
});

describe("MessageGroup attachments", () => {
  it("opens the full-screen viewer when an attachment is tapped", async () => {
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment] }}
        collapsed={false}
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
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
       
        currentUserId="user-a"
        onLongPress={onLongPress}
        attachmentUrl={attachmentUrl}
      />,
    );
    // The author name is a plain Text node (unlike the markdown body), so this
    // long-press bubbles to the group's own Pressable without depending on the
    // markdown renderer's internal tree shape.
    await fireEvent(screen.getByText("Friday"), "longPress");
    expect(onLongPress).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Close")).toBeNull();
    // Fires exactly once, and strictly before the caller's own handler (same
    // frame as the actions sheet is about to present, not after it mounts).
    expect(Haptics.impactAsync).toHaveBeenCalledTimes(1);
    expect(Haptics.impactAsync).toHaveBeenCalledWith(Haptics.ImpactFeedbackStyle.Medium);
    const hapticOrder = (Haptics.impactAsync as jest.Mock).mock.invocationCallOrder[0];
    const longPressOrder = onLongPress.mock.invocationCallOrder[0];
    expect(hapticOrder).toBeLessThan(longPressOrder);
  });

  it("pages between several attachments with an n of m indicator", async () => {
    const second: MessageAttachment = { ...photoAttachment, id: "att-2", fileName: "beach.png" };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photoAttachment, second] }}
        collapsed={false}
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open sunset.png"));
    expect(screen.getByText("1 of 2")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Next attachment"));
    expect(screen.getByText("2 of 2")).toBeTruthy();
    expect(screen.getByText("beach.png")).toBeTruthy();
  });

  it("falls back to a share card for a non-image, non-PDF, non-video attachment", async () => {
    const archive: MessageAttachment = {
      ...photoAttachment,
      id: "att-3",
      fileName: "demo.zip",
      contentType: "application/zip",
    };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [archive] }}
        collapsed={false}
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open demo.zip"));
    // The share-card action carries its own, differently-worded label so this
    // assertion can't accidentally match the still-mounted list row's tap target.
    expect(await screen.findByLabelText("Open demo.zip in another app")).toBeTruthy();
    expect(screen.getByText("2.0 KB · application/zip")).toBeTruthy();
  });

  // Through a Blob it never drew on a phone: React Native cannot build one from
  // bytes, which the old fixture (a Node Blob) could not show.
  it("draws an image from the file its signed link downloads to", async () => {
    attachmentUrl.mockClear();
    const photo: MessageAttachment = { ...photoAttachment, id: "att-9", fileName: "Screenshot at 2.10.41\u202fPM.png" };
    await render(
      <MessageGroup
        message={{ ...message, attachments: [photo] }}
        collapsed={false}
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
      />,
    );
    await waitFor(() => expect(attachmentUrl).toHaveBeenCalledWith("room-1", "att-9"));
    // One download serves the thumbnail and the viewer.
    await fireEvent.press(screen.getByLabelText("Open Screenshot at 2.10.41\u202fPM.png"));
    expect(await screen.findByLabelText("Close")).toBeTruthy();
    expect(screen.queryByText(/Could not load/)).toBeNull();
    expect(attachmentUrl).toHaveBeenCalledTimes(1);
  });

  it("plays a video straight from its presigned URL", async () => {
    const video: MessageAttachment = {
      ...photoAttachment,
      id: "att-5",
      fileName: "demo.mov",
      contentType: "video/quicktime",
    };
    attachmentUrl.mockClear();
    await render(
      <MessageGroup
        message={{ ...message, attachments: [video] }}
        collapsed={false}
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
      />,
    );
    // The thumbnail row itself never fetches anything for a video.
    expect(attachmentUrl).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByLabelText("Open demo.mov"));
    const player = await screen.findByTestId("mock-webview");
    expect(attachmentUrl).toHaveBeenCalledWith("room-1", "att-5");
    expect(player.props.accessibilityLabel).toBe("https://s3.example/video.mp4?sig=x");
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
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
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
       
        currentUserId="user-a"
        attachmentUrl={attachmentUrl}
      />,
    );
    await fireEvent.press(screen.getByLabelText("Open sunset.png"));
    expect(screen.queryByLabelText("Close")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Close"));
    expect(screen.queryByLabelText("Close")).toBeNull();
  });
});
