import { render } from "@testing-library/react-native";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";

import { NotificationTapRouter, notificationHref } from "./NotificationTapRouter";

jest.mock("expo-router", () => ({ router: { navigate: jest.fn() } }));

const lastResponse = Notifications.useLastNotificationResponse as jest.Mock;
const clear = Notifications.clearLastNotificationResponse as jest.Mock;
const navigate = router.navigate as jest.Mock;

// The router remembers the last notification id it routed (module scope), so
// every test taps a notification of its own.
let seq = 0;
function response(data: Record<string, unknown>, id = `n${++seq}`) {
  return { notification: { request: { identifier: id, content: { data } } } };
}

beforeEach(() => {
  jest.clearAllMocks();
  lastResponse.mockReturnValue(null);
});

it("opens Needs you for anything that asks the person to decide", () => {
  expect(notificationHref({ source: "task_question", taskId: "t-1", roomId: "r-1" })).toBe("/(app)/(tabs)/activity");
  expect(notificationHref({ source: "teams", approvalId: "ap-9", issueId: "i" })).toBe("/(app)/(tabs)/activity");
  expect(notificationHref({ source: "teams", interactionId: "int-1", issueId: "iss-4" })).toBe("/(app)/(tabs)/activity");
});

it("opens Home for a room, session or legacy agent push", () => {
  expect(notificationHref({ roomId: "room-1" })).toBe("/(app)/(tabs)/chats");
  expect(notificationHref({ source: "session", sessionId: "s-1" })).toBe("/(app)/(tabs)/chats");
  expect(notificationHref({ agentId: "agent_1" })).toBe("/(app)/(tabs)/chats");
});

it("ignores a payload that names nothing it knows", () => {
  expect(notificationHref({ issueId: "issue_abc" })).toBeNull();
  expect(notificationHref({ source: "teams", issueId: "issue_abc" })).toBeNull();
  expect(notificationHref(undefined)).toBeNull();
});

it("does nothing when the app was not opened from a notification", async () => {
  await render(<NotificationTapRouter />);
  expect(navigate).not.toHaveBeenCalled();
  expect(clear).not.toHaveBeenCalled();
});

// The warm path: the hook starts at null and flips to the tapped response.
it("routes a tap that arrives after mount", async () => {
  const screen = await render(<NotificationTapRouter />);
  lastResponse.mockReturnValue(response({ source: "task_question", taskId: "t-1" }));
  await screen.rerender(<NotificationTapRouter />);
  expect(navigate.mock.calls).toEqual([["/(app)/(tabs)/activity"]]);
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
  expect(navigate).toHaveBeenCalledTimes(1);
});
