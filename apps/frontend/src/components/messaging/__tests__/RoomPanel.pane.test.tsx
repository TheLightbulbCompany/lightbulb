// The conversation's tools and side column in the shell's one right pane.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import type { RoomMessage, RoomParticipant } from "../types";
import { _resetControllerForTests, attachShell, getWorkspaceSnapshot, setThreadPaneHost } from "@/components/workspace/workspaceController";

const shared = vi.hoisted(() => ({ statusLoaded: true }));
vi.mock("@lightbulb/shared", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useStatusLoaded: () => shared.statusLoaded,
}));
vi.mock("../useMessageSearch", () => ({ useMessageSearch: () => ({ hits: [], state: "idle" }) }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
vi.mock("@/lib/shellNav", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
const reply: RoomMessage = { ...message, messageId: "reply", messageSeq: 2, text: "Reply text", threadRootMessageId: "message" };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 2, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [owner, friday], messages: [message, reply],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn(), focusAgentId: "friday" },
};

beforeEach(() => {
  _resetControllerForTests();
  shared.statusLoaded = true;
});

it("offers Browser and Files in the conversation header only inside the shell, each opening the pane", () => {
  const outside = render(<RoomPanel {...props} />);
  expect(screen.queryByRole("button", { name: "Files" })).toBeNull();
  outside.unmount();

  attachShell(() => undefined);
  render(<RoomPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Files" }));
  expect(getWorkspaceSnapshot().pane).toMatchObject({ panelId: "file-viewer", params: { agent: "friday" } });
  fireEvent.click(screen.getByRole("button", { name: "Browser" }));
  expect(getWorkspaceSnapshot().pane?.panelId).toBe("browser");
  expect(getWorkspaceSnapshot().place).toBe("chat");
});

it("offers Ideas in the conversation header, opening the pane with the DM's agent first", () => {
  attachShell(() => undefined);
  render(<RoomPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Ideas" }));
  expect(getWorkspaceSnapshot().pane).toMatchObject({ panelId: "ideas", component: "ideas", params: { agent: "friday" } });
  expect(getWorkspaceSnapshot().place).toBe("chat");
});

it("hides Browser until a status body has landed", () => {
  shared.statusLoaded = false;
  attachShell(() => undefined);
  render(<RoomPanel {...props} />);
  expect(screen.queryByRole("button", { name: "Browser" })).toBeNull();
  expect(screen.getByRole("button", { name: "Files" })).toBeInTheDocument();
});

it("shows a thread in the pane, and folds it when something else takes the pane", () => {
  attachShell(() => undefined);
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => setThreadPaneHost(host));
  render(<RoomPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Reply in thread" }));
  expect(getWorkspaceSnapshot().pane?.component).toBe("thread");
  expect(within(host).getByRole("complementary", { name: "Thread" })).toHaveTextContent("Reply text");
  fireEvent.click(screen.getByRole("button", { name: "Files" }));
  expect(getWorkspaceSnapshot().pane?.panelId).toBe("file-viewer");
  expect(screen.queryByRole("complementary", { name: "Thread" })).toBeNull();
  host.remove();
});
