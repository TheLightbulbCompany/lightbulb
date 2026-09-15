import { render } from "@testing-library/react-native";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";

import { claimRoomTarget } from "../../lib/activeAgent";
import { NotificationTapRouter } from "./NotificationTapRouter";

jest.mock("expo-router", () => ({
  router: { push: jest.fn(), dismissAll: jest.fn(), canDismiss: jest.fn(() => false) },
}));

const lastResponse = Notifications.useLastNotificationResponse as jest.Mock;
const clear = Notifications.clearLastNotificationResponse as jest.Mock;
const push = router.push as jest.Mock;
const dismissAll = router.dismissAll as jest.Mock;
const canDismiss = router.canDismiss as jest.Mock;

async function tap(data: Record<string, unknown>) {
  lastResponse.mockReturnValue({ notification: { request: { content: { data } } } });
  // RNTL 14's render() is async — await it before asserting effects.
  await render(<NotificationTapRouter />);
}

beforeEach(() => {
  jest.clearAllMocks();
  lastResponse.mockReturnValue(null);
  canDismiss.mockReturnValue(false);
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

// The double-back bug: every tap pushed, so the reader's back button walked
// through whatever was open before, including a second copy of the room.
it("unwinds to the tabs before pushing, so back always leads home", async () => {
  canDismiss.mockReturnValue(true);
  await tap({ roomId: "room-2" });
  expect(dismissAll).toHaveBeenCalledTimes(1);
  expect(dismissAll.mock.invocationCallOrder[0]).toBeLessThan(push.mock.invocationCallOrder[0]);
});

it("does not push the conversation the reader already has open", async () => {
  const release = claimRoomTarget("room-1", null);
  await tap({ roomId: "room-1", threadRootMessageId: null });
  release();
  expect(push).not.toHaveBeenCalled();
  expect(dismissAll).not.toHaveBeenCalled();
});

// The cold-start loop: the native module returns the last tap until cleared,
// so every remount re-routed it.
it("consumes the response so a remount cannot route it again", async () => {
  await tap({ roomId: "room-1" });
  expect(clear).toHaveBeenCalledTimes(1);
});
