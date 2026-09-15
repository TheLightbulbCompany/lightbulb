import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const mocks = vi.hoisted(() => ({
  handlers: new Set<(event: LiveEvent) => void>(),
  getRoom: vi.fn(), listMessages: vi.fn(), nativeNotify: vi.fn(), notifySuccess: vi.fn(), capture: vi.fn(),
  notificationPermission: vi.fn(), isDesktopApp: vi.fn(), notifyAttention: vi.fn(),
}));
const messaging = { getRoom: mocks.getRoom, listMessages: mocks.listMessages };
const onEvent = (handler: (event: LiveEvent) => void) => { mocks.handlers.add(handler); return () => mocks.handlers.delete(handler); };
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "me", orgId: "org" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent, messaging }) }));
vi.mock("@/lib/analytics", () => ({ capture: mocks.capture }));
vi.mock("@/lib/desktopNotify", () => ({
  nativeNotify: mocks.nativeNotify, notificationPermission: mocks.notificationPermission, isDesktopApp: mocks.isDesktopApp,
}));
vi.mock("@/lib/notify", () => ({ notifySuccess: mocks.notifySuccess, notifyAttention: mocks.notifyAttention }));
import { MessageNotifications } from "../MessageNotifications";

const event: LiveEvent = {
  type: "room.message.available", schemaVersion: 1, eventId: "hint", occurredAt: "2026-09-10T00:00:00Z", roomId: "room",
  payload: { roomId: "room", messageId: "message", messageSeq: 2, threadRootMessageId: null, notify: true },
};
const emit = () => act(() => { for (const handler of mocks.handlers) handler(event); });
beforeEach(() => {
  mocks.handlers.clear();
  vi.clearAllMocks();
  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  mocks.nativeNotify.mockResolvedValue(false);
  mocks.notificationPermission.mockResolvedValue(null);
  mocks.isDesktopApp.mockReturnValue(false);
  localStorage.clear();
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 0 });
  mocks.listMessages.mockResolvedValue({ messages: [{ messageId: "message", text: "Hello", author: { kind: "human", id: "other", displayName: "Alice" } }] });
});

it("notifies background DMs once and exposes a conversation action", async () => {
  const onOpen = vi.fn();
  render(<MessageNotifications activeRoomId="different" threadId={null} roomVisible onOpen={onOpen} />);
  emit(); emit();
  await waitFor(() => expect(mocks.notifySuccess).toHaveBeenCalledTimes(1));
  expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
  const [title, body, opts] = mocks.nativeNotify.mock.calls[0];
  expect(title).toBe("Alice");
  expect(body).toBe("Hello");
  expect(opts).toMatchObject({ tag: "message" });
  expect(mocks.capture).toHaveBeenCalledWith("notification_shown", { channel: "web", room_kind: "dm" });
  mocks.notifySuccess.mock.calls[0][1].action.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null);
  expect(mocks.capture).toHaveBeenCalledWith("notification_clicked", { channel: "web", room_kind: "dm" });
  onOpen.mockClear();
  opts.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null);
  expect(mocks.capture.mock.calls.filter((c) => c[0] === "notification_clicked")).toHaveLength(2);
});

it("shows only the native notification when one is available — no toast", async () => {
  mocks.nativeNotify.mockResolvedValue(true);
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
});

it("falls back to the in-app toast when no native notification is available", async () => {
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).toHaveBeenCalledWith("Alice: Hello", expect.objectContaining({ dedupeKey: "message:message" }));
});

it("tags notification_shown with the room's kind, not just dm", async () => {
  mocks.getRoom.mockResolvedValue({ kind: "channel", name: "general", lastReadSeq: 0 });
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.capture).toHaveBeenCalledWith("notification_shown", { channel: "web", room_kind: "channel" });
});

it("suppresses a focused conversation but not an unopened thread in it", async () => {
  vi.mocked(document.hasFocus).mockReturnValue(true);
  const view = render(<MessageNotifications activeRoomId="room" threadId={null} roomVisible onOpen={vi.fn()} />);
  emit();
  expect(mocks.getRoom).not.toHaveBeenCalled();
  view.unmount();
  render(<MessageNotifications activeRoomId="room" threadId={null} roomVisible onOpen={vi.fn()} />);
  act(() => { for (const handler of mocks.handlers) handler({ ...event, payload: { ...event.payload, threadRootMessageId: "root" } }); });
  await waitFor(() => expect(mocks.notifySuccess).toHaveBeenCalledTimes(1));
});

it("does not show content after access is denied or for self messages", async () => {
  mocks.getRoom.mockRejectedValueOnce(new Error("403"));
  const view = render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  view.unmount();
  mocks.listMessages.mockResolvedValue({ messages: [{ messageId: "message", text: "Mine", author: { kind: "human", id: "me", displayName: "Me" } }] });
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
});

it("stays silent for a muted room but notifies for an unmuted one", async () => {
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 0, muted: true });
  const view = render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  expect(mocks.capture).not.toHaveBeenCalled();
  view.unmount();
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 0, muted: false });
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).toHaveBeenCalledTimes(1);
  expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
});

it("suppresses delayed hints for a message already read on another client", async () => {
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 2 });
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
});

describe("asking to turn notifications on", () => {
  const renderIt = () => render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);

  it("asks once, and Turn on requests the permission", async () => {
    mocks.notificationPermission.mockResolvedValue("default");
    const view = renderIt();
    await waitFor(() => expect(mocks.notifyAttention).toHaveBeenCalledTimes(1));
    const [message, opts] = mocks.notifyAttention.mock.calls[0];
    expect(message).toMatch(/notified/i);
    expect(opts.action.label).toBe("Turn on");
    expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
    opts.action.onClick();
    expect(mocks.notificationPermission).toHaveBeenCalledWith(true);
    view.unmount();
    renderIt();
    await act(async () => {});
    expect(mocks.notifyAttention).toHaveBeenCalledTimes(1);
  });

  it("does not ask when notifications are already on", async () => {
    mocks.notificationPermission.mockResolvedValue("granted");
    renderIt();
    await act(async () => {});
    expect(mocks.notifyAttention).not.toHaveBeenCalled();
  });

  it("leaves a browser that was told no alone — it cannot be reopened from the page", async () => {
    mocks.notificationPermission.mockResolvedValue("denied");
    renderIt();
    await act(async () => {});
    expect(mocks.notifyAttention).not.toHaveBeenCalled();
  });

  it("asks in the desktop app after an earlier no, since Turn on opens System Settings", async () => {
    mocks.notificationPermission.mockResolvedValue("denied");
    mocks.isDesktopApp.mockReturnValue(true);
    renderIt();
    await waitFor(() => expect(mocks.notifyAttention).toHaveBeenCalledTimes(1));
  });

  it("waits for a later visit while there is no native path (an older desktop build)", async () => {
    const view = renderIt();
    await act(async () => {});
    expect(mocks.notifyAttention).not.toHaveBeenCalled();
    view.unmount();
    mocks.notificationPermission.mockResolvedValue("default");
    renderIt();
    await waitFor(() => expect(mocks.notifyAttention).toHaveBeenCalledTimes(1));
  });
});
