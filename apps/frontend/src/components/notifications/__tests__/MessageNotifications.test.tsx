import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";

const mocks = vi.hoisted(() => ({
  handlers: new Set<(event: LiveEvent) => void>(),
  nativeNotify: vi.fn(), notifySuccess: vi.fn(), capture: vi.fn(),
  notificationPermission: vi.fn(), isDesktopApp: vi.fn(), notifyAttention: vi.fn(),
  sessionClick: null as ((sessionId: string) => void) | null,
}));
const onEvent = (handler: (event: LiveEvent) => void) => { mocks.handlers.add(handler); return () => mocks.handlers.delete(handler); };
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "me" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent }) }));
vi.mock("@/lib/analytics", () => ({ capture: mocks.capture }));
vi.mock("@/lib/desktopNotify", () => ({
  nativeNotify: mocks.nativeNotify, notificationPermission: mocks.notificationPermission, isDesktopApp: mocks.isDesktopApp,
  onDesktopToast: () => () => {},
  desktopDeviceId: async () => null,
  onSessionNoticeClick: (handler: (sessionId: string) => void) => { mocks.sessionClick = handler; return () => { mocks.sessionClick = null; }; },
}));
vi.mock("@/lib/notify", () => ({ notifySuccess: mocks.notifySuccess, notifyAttention: mocks.notifyAttention }));
import { MessageNotifications } from "../MessageNotifications";

const push = { title: "Alice", subtitle: "Direct message", body: "Hello", threadId: "room",
  data: { roomId: "room", messageId: "message", messageSeq: 2, threadRootMessageId: null, roomKind: "dm" } };
const event: LiveEvent = {
  type: "room.message.available", schemaVersion: 1, eventId: "hint", occurredAt: "2026-09-10T00:00:00Z", roomId: "room",
  payload: { roomId: "room", messageId: "message", messageSeq: 2, threadRootMessageId: null, notify: true, push },
};
const emit = (e: LiveEvent = event) => act(() => { for (const handler of mocks.handlers) handler(e); });
const renderAway = () => render(<MessageNotifications activeRoomId={null} roomVisible={false} onOpen={vi.fn()} />);
beforeEach(() => {
  mocks.handlers.clear();
  vi.clearAllMocks();
  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  mocks.nativeNotify.mockResolvedValue(false);
  mocks.notificationPermission.mockResolvedValue(null);
  mocks.isDesktopApp.mockReturnValue(false);
  localStorage.clear();
});

it("shows the projector's banner verbatim, once, with a conversation action", async () => {
  const onOpen = vi.fn();
  render(<MessageNotifications activeRoomId="different" roomVisible onOpen={onOpen} />);
  emit(); emit();
  await waitFor(() => expect(mocks.notifySuccess).toHaveBeenCalledTimes(1));
  expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
  const [title, body, opts] = mocks.nativeNotify.mock.calls[0];
  expect([title, body]).toEqual(["Alice", "Hello"]);
  expect(opts).toMatchObject({ tag: "message", subtitle: "Direct message", threadId: "room" });
  expect(mocks.capture).toHaveBeenCalledWith("notification_shown", { channel: "web", room_kind: "dm" });
  mocks.notifySuccess.mock.calls[0][1].action.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null, "message");
  expect(mocks.capture).toHaveBeenCalledWith("notification_clicked", { channel: "web", room_kind: "dm" });
  onOpen.mockClear();
  opts.onClick();
  expect(onOpen).toHaveBeenCalledWith("room", null, "message");
});

it("shows only the native notification when one is available — no toast", async () => {
  mocks.nativeNotify.mockResolvedValue(true);
  renderAway();
  await act(async () => { emit(); });
  expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
});

it("falls back to the in-app toast when no native notification is available", async () => {
  renderAway();
  await act(async () => { emit(); });
  expect(mocks.notifySuccess).toHaveBeenCalledWith("Alice · Direct message",
    expect.objectContaining({ dedupeKey: "message:message", description: "Hello" }));
});

it("stays silent on a hint without a banner (not notifying, muted, own message)", async () => {
  renderAway();
  await act(async () => { emit({ ...event, payload: { ...event.payload, push: undefined } } as LiveEvent); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
  expect(mocks.capture).not.toHaveBeenCalled();
});

it("ignores a malformed banner rather than showing half of it", async () => {
  renderAway();
  await act(async () => { emit({ ...event, payload: { ...event.payload, push: true } } as unknown as LiveEvent); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
});

it("stays quiet for an agent reply whose run already notified", async () => {
  const { claimRunNotice } = await import("../roomView");
  expect(claimRunNotice("run-msg-claimed")).toBe(true); // RunNotifications got there first
  renderAway();
  await act(async () => { emit({ ...event, payload: { ...event.payload, push: { ...push, data: { ...push.data, runId: "run-msg-claimed" } } } } as LiveEvent); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
});

it("suppresses a focused conversation but not an unopened thread in it", async () => {
  vi.mocked(document.hasFocus).mockReturnValue(true);
  const view = render(<MessageNotifications activeRoomId="room" roomVisible onOpen={vi.fn()} />);
  await act(async () => { emit(); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  view.unmount();
  render(<MessageNotifications activeRoomId="room" roomVisible onOpen={vi.fn()} />);
  emit({ ...event, payload: { ...event.payload, threadRootMessageId: "root" } } as LiveEvent);
  await waitFor(() => expect(mocks.notifySuccess).toHaveBeenCalledTimes(1));
});

it("suppresses thread replies too in a side room, which shows replies inline (a session's chat)", async () => {
  vi.mocked(document.hasFocus).mockReturnValue(true);
  render(<MessageNotifications activeRoomId="main" roomVisible sideRoomIds={["room"]} onOpen={vi.fn()} />);
  await act(async () => { emit({ ...event, payload: { ...event.payload, threadRootMessageId: "root" } } as LiveEvent); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  expect(mocks.notifySuccess).not.toHaveBeenCalled();
});

it("keeps the main room and every side room quiet at once, and toasts a room no longer on screen", async () => {
  vi.mocked(document.hasFocus).mockReturnValue(true);
  const at = (roomId: string, messageId: string) => ({ ...event, payload: { ...event.payload, roomId, messageId } } as LiveEvent);
  const view = render(<MessageNotifications activeRoomId="main" roomVisible sideRoomIds={["chat", "conv"]} onOpen={vi.fn()} />);
  await act(async () => { emit(at("main", "m1")); emit(at("chat", "m2")); emit(at("conv", "m3")); });
  expect(mocks.nativeNotify).not.toHaveBeenCalled();
  // The app's conversation is hidden (its pane is not shown): it unregisters, and its messages notify again.
  view.rerender(<MessageNotifications activeRoomId="main" roomVisible sideRoomIds={["chat"]} onOpen={vi.fn()} />);
  await act(async () => { emit(at("conv", "m4")); });
  await waitFor(() => expect(mocks.nativeNotify).toHaveBeenCalledTimes(1));
});

describe("asking for notification permission", () => {
  const renderIt = renderAway;

  it("raises the macOS prompt itself in the desktop app, once per person on this device", async () => {
    mocks.notificationPermission.mockResolvedValue("default");
    mocks.isDesktopApp.mockReturnValue(true);
    const view = renderIt();
    await waitFor(() => expect(mocks.notificationPermission).toHaveBeenCalledWith(true));
    expect(mocks.notifyAttention).not.toHaveBeenCalled(); // no in-app ask in front of it
    view.unmount();
    mocks.notificationPermission.mockClear();
    renderIt();
    await act(async () => {});
    expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
  });

  it("waits for a user gesture in a browser, which is all its prompt needs", async () => {
    mocks.notificationPermission.mockResolvedValue("default");
    renderIt();
    await act(async () => {});
    expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
    document.dispatchEvent(new Event("pointerdown"));
    expect(mocks.notificationPermission).toHaveBeenCalledWith(true);
    document.dispatchEvent(new Event("pointerdown"));
    document.dispatchEvent(new Event("keydown"));
    expect(mocks.notificationPermission.mock.calls.filter(([request]) => request === true)).toHaveLength(1);
  });

  it("stops listening for the gesture once unmounted", async () => {
    mocks.notificationPermission.mockResolvedValue("default");
    const view = renderIt();
    await act(async () => {});
    view.unmount();
    document.dispatchEvent(new Event("pointerdown"));
    expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
  });

  it("never asks again once the answer is in, either way", async () => {
    for (const state of ["granted", "denied"] as const) {
      mocks.notificationPermission.mockReset().mockResolvedValue(state);
      for (const desktop of [true, false]) {
        mocks.isDesktopApp.mockReturnValue(desktop);
        const view = renderIt();
        await act(async () => {});
        document.dispatchEvent(new Event("pointerdown"));
        expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
        view.unmount();
      }
    }
  });

  it("tries again on a later visit while there is no native path (an older desktop build)", async () => {
    mocks.isDesktopApp.mockReturnValue(true);
    const view = renderIt();
    await act(async () => {});
    expect(mocks.notificationPermission).not.toHaveBeenCalledWith(true);
    view.unmount();
    mocks.notificationPermission.mockResolvedValue("default");
    renderIt();
    await waitFor(() => expect(mocks.notificationPermission).toHaveBeenCalledWith(true));
  });
});

describe("shared session attention", () => {
  const attention: LiveEvent = {
    type: "workspace.session.attention", schemaVersion: 1, eventId: "att-1", occurredAt: "2026-09-25T00:00:00Z",
    payload: { sessionId: "s1", state: "waiting", push: { title: "Claude · Fix login is waiting for you", body: "Approve or answer it in your terminal.", data: { source: "session", sessionId: "s1" } } },
  };

  it("notifies once with the server's copy and opens the session on click", async () => {
    const onOpenSession = vi.fn();
    render(<MessageNotifications activeRoomId={null} roomVisible={false} onOpen={vi.fn()} onOpenSession={onOpenSession} />);
    await act(async () => { emit(attention); emit(attention); });
    expect(mocks.nativeNotify).toHaveBeenCalledTimes(1);
    const [title, body, opts] = mocks.nativeNotify.mock.calls[0];
    expect([title, body, opts.tag]).toEqual(["Claude · Fix login is waiting for you", "Approve or answer it in your terminal.", "att-1"]);
    await waitFor(() => expect(mocks.notifySuccess).toHaveBeenCalledTimes(1));
    opts.onClick();
    expect(onOpenSession).toHaveBeenCalledWith("s1");
  });

  it("stays quiet while that session is on screen and focused", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    render(<MessageNotifications activeRoomId={null} roomVisible={false} onOpen={vi.fn()} activeSessionId="s1" />);
    await act(async () => { emit(attention); });
    expect(mocks.nativeNotify).not.toHaveBeenCalled();
  });

  it("follows the owner's prefs: no banner below Banner; only-unfocused holds it while focused", async () => {
    const at = (eventId: string, extra: object) =>
      ({ ...attention, eventId, payload: { ...attention.payload, ...extra } }) as LiveEvent;
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    render(<MessageNotifications activeRoomId={null} roomVisible={false} onOpen={vi.fn()} />);
    await act(async () => {
      emit(at("a", { level: "badge" }));
      emit(at("b", { level: "off" }));
      emit(at("c", { level: "banner", onlyUnfocused: true }));
    });
    expect(mocks.nativeNotify).not.toHaveBeenCalled();
    expect(mocks.notifySuccess).not.toHaveBeenCalled();
    await act(async () => { emit(at("d", { level: "banner", sound: false, onlyUnfocused: false })); });
    expect(mocks.nativeNotify.mock.calls[0][2]).toMatchObject({ tag: "d", sound: false, onlyUnfocused: false });
  });
});

it("a click on a Mac request notification opens the session it is about", () => {
  const onOpenSession = vi.fn();
  render(<MessageNotifications activeRoomId={null} roomVisible={false} onOpen={vi.fn()} onOpenSession={onOpenSession} />);
  mocks.sessionClick?.("s-1");
  expect(onOpenSession).toHaveBeenCalledWith("s-1");
});
