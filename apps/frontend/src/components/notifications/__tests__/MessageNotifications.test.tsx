import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const mocks = vi.hoisted(() => ({
  handlers: new Set<(event: LiveEvent) => void>(),
  getRoom: vi.fn(), listMessages: vi.fn(), nativeNotify: vi.fn(), notifySuccess: vi.fn(),
}));
const messaging = { getRoom: mocks.getRoom, listMessages: mocks.listMessages };
const onEvent = (handler: (event: LiveEvent) => void) => { mocks.handlers.add(handler); return () => mocks.handlers.delete(handler); };
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "me", orgId: "org" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent, messaging }) }));
vi.mock("@/lib/desktopNotify", () => ({ nativeNotify: mocks.nativeNotify }));
vi.mock("@/lib/notify", () => ({ notifySuccess: mocks.notifySuccess }));
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
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 0 });
  mocks.listMessages.mockResolvedValue({ messages: [{ messageId: "message", text: "Hello", author: { kind: "human", id: "other", displayName: "Alice" } }] });
});

it("notifies background DMs once and exposes a conversation action", async () => {
  const onOpen = vi.fn();
  render(<MessageNotifications activeRoomId="different" threadId={null} roomVisible onOpen={onOpen} />);
  emit(); emit();
  await waitFor(() => expect(mocks.nativeNotify).toHaveBeenCalledTimes(1));
  const [title, body, opts] = mocks.nativeNotify.mock.calls[0];
  expect(title).toBe("Alice");
  expect(body).toBe("Hello");
  expect(opts).toMatchObject({ tag: "message" });
  mocks.notifySuccess.mock.calls[0][1].action.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null);
  onOpen.mockClear();
  opts.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null);
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

it("suppresses delayed hints for a message already read on another client", async () => {
  mocks.getRoom.mockResolvedValue({ kind: "dm", lastReadSeq: 2 });
  render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
});

describe("notification-permission priming", () => {
  class FakeNotification {
    static permission: NotificationPermission = "default";
    static requestPermission = vi.fn().mockResolvedValue("granted");
  }

  beforeEach(() => {
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockClear();
    vi.stubGlobal("Notification", FakeNotification);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("requests permission on the first pointerdown, then stops listening", () => {
    render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
    document.dispatchEvent(new Event("pointerdown"));
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new Event("pointerdown"));
    document.dispatchEvent(new Event("keydown"));
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
  });

  it("requests permission on the first keydown too", () => {
    render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
    document.dispatchEvent(new Event("keydown"));
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
  });

  it("does not request when permission is already decided", () => {
    FakeNotification.permission = "granted";
    render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />);
    document.dispatchEvent(new Event("pointerdown"));
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("does not throw when the Notification API is missing", () => {
    vi.unstubAllGlobals();
    expect(() => render(<MessageNotifications activeRoomId={null} threadId={null} roomVisible={false} onOpen={vi.fn()} />)).not.toThrow();
    expect(() => document.dispatchEvent(new Event("pointerdown"))).not.toThrow();
  });
});
