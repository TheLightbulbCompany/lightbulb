import { render } from "@testing-library/react-native";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";

import { claimRoomTarget } from "../../lib/activeAgent";
import { NotificationTapRouter } from "./NotificationTapRouter";

jest.mock("expo-router", () => ({ router: { push: jest.fn(), dismissTo: jest.fn() } }));

const lastResponse = Notifications.useLastNotificationResponse as jest.Mock;
const clear = Notifications.clearLastNotificationResponse as jest.Mock;
const push = router.push as jest.Mock;
const dismissTo = router.dismissTo as jest.Mock;

// The router remembers the last notification id it routed (module scope), so
// every test taps a notification of its own.
let seq = 0;
function response(data: Record<string, unknown>, id = `n${++seq}`) {
  return { notification: { request: { identifier: id, content: { data } } } };
}

async function tap(data: Record<string, unknown>) {
  lastResponse.mockReturnValue(response(data));
  // RNTL 14's render() is async — await it before asserting effects.
  return render(<NotificationTapRouter />);
}

/** Put a room (or one of its threads) on screen for the duration of a test. */
let release: (() => void) | null = null;
function reading(roomId: string, threadId: string | null = null) {
  release = claimRoomTarget(roomId, threadId);
}

beforeEach(() => {
  jest.clearAllMocks();
  lastResponse.mockReturnValue(null);
});
afterEach(() => {
  release?.();
  release = null;
});

it("does nothing when the app was not opened from a notification", async () => {
  await render(<NotificationTapRouter />);
  expect(push).not.toHaveBeenCalled();
  expect(clear).not.toHaveBeenCalled();
});

it("routes a room push to the conversation", async () => {
  await tap({ roomId: "room-1", threadRootMessageId: null });
  expect(push.mock.calls).toEqual([["/(app)/room/room-1"]]);
});

it("opens a thread reply over its room, as Reply in thread does", async () => {
  await tap({ roomId: "room-1", threadRootMessageId: "m1" });
  expect(push.mock.calls).toEqual([["/(app)/room/room-1"], ["/(app)/room/room-1/thread/m1"]]);
});

it("routes a Teams push to its issue, whatever the event type", async () => {
  await tap({ source: "teams", eventType: "approval.created", issueId: "issue_xyz" });
  expect(push).toHaveBeenCalledWith({
    pathname: "/(app)/teams/issue/[id]",
    params: { id: "issue_xyz" },
  });
});

it("ignores an issueId that did not come from Teams", async () => {
  await tap({ issueId: "issue_abc" });
  expect(push).not.toHaveBeenCalled();
});

it("still routes a legacy agent push to its chat", async () => {
  await tap({ agentId: "agent_1", sessionKey: "sess:abc" });
  expect(push).toHaveBeenCalledWith("/agent/agent_1");
});

// The warm path: the hook starts at null and flips to the tapped response.
it("routes a tap that arrives after mount", async () => {
  const screen = await render(<NotificationTapRouter />);
  lastResponse.mockReturnValue(response({ roomId: "room-9" }));
  await screen.rerender(<NotificationTapRouter />);
  expect(push.mock.calls).toEqual([["/(app)/room/room-9"]]);
});

// The double-back bug: a push for the room being read stacked a second copy.
it("does not push the conversation the reader already has open", async () => {
  reading("room-1");
  await tap({ roomId: "room-1", threadRootMessageId: null });
  expect(push).not.toHaveBeenCalled();
  expect(dismissTo).not.toHaveBeenCalled();
});

it("opens a thread reply over its room when that room is on screen", async () => {
  reading("room-1");
  await tap({ roomId: "room-1", threadRootMessageId: "m1" });
  expect(push.mock.calls).toEqual([["/(app)/room/room-1/thread/m1"]]);
});

it("goes back down to the room from one of its threads, not a second copy", async () => {
  reading("room-1", "m1");
  await tap({ roomId: "room-1", threadRootMessageId: null });
  expect(dismissTo).toHaveBeenCalledWith("/(app)/room/room-1");
  expect(push).not.toHaveBeenCalled();
});

// Unwinding to the tabs would unmount the open room and its unsent draft.
it("pushes over another open conversation instead of unwinding it", async () => {
  reading("room-2");
  await tap({ roomId: "room-1" });
  expect(push.mock.calls).toEqual([["/(app)/room/room-1"]]);
  expect(dismissTo).not.toHaveBeenCalled();
});

// The cold-start loop: the native module returns the last tap until cleared,
// and a tap landing mid-mount is seen twice. Either way it routes once.
it("consumes the response and routes a notification only once", async () => {
  const same = response({ roomId: "room-1" });
  lastResponse.mockReturnValue(same);
  const first = await render(<NotificationTapRouter />);
  expect(clear).toHaveBeenCalledTimes(1);
  first.unmount();

  await render(<NotificationTapRouter />);
  expect(push).toHaveBeenCalledTimes(1);
});
