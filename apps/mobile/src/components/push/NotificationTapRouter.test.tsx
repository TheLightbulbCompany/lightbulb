import { render } from "@testing-library/react-native";
import * as Notifications from "expo-notifications";
import { router } from "expo-router";

import { NotificationTapRouter, notificationHref } from "./NotificationTapRouter";

jest.mock("expo-router", () => ({ router: { navigate: jest.fn(), push: jest.fn() } }));
let mockRooms: unknown[] = [];
let mockRoomsLoading = false;
jest.mock("../../lib/rooms", () => ({ useMobileRooms: () => ({ rooms: mockRooms, roomsLoading: mockRoomsLoading }) }));
jest.mock("../../providers/live-events", () => ({ useLiveEvents: () => ({ frontAgentId: "front" }) }));
const CHAT = { roomId: "chat-1", kind: "group-dm", members: [{ kind: "agent", id: "front" }, { kind: "human", id: "me" }] };
const CALL = { roomId: "call-1", kind: "channel", members: [{ kind: "agent", id: "front" }] };

const lastResponse = Notifications.useLastNotificationResponse as jest.Mock;
const clear = Notifications.clearLastNotificationResponse as jest.Mock;
const navigate = router.navigate as jest.Mock;
const push = router.push as jest.Mock;

// The router remembers the last notification id it routed (module scope), so
// every test taps a notification of its own.
let seq = 0;
function response(data: Record<string, unknown>, id = `n${++seq}`) {
  return { notification: { request: { identifier: id, content: { data } } } };
}

beforeEach(() => {
  jest.clearAllMocks();
  lastResponse.mockReturnValue(null);
  mockRooms = [CHAT, CALL];
  mockRoomsLoading = false;
});

it("opens Home and then the chat for a push about one of the member's text chats", async () => {
  lastResponse.mockReturnValue(response({ roomId: "chat-1" }));
  await render(<NotificationTapRouter />);
  expect(navigate.mock.calls).toEqual([["/(app)/(tabs)/chats"]]);
  expect(push.mock.calls).toEqual([["/(app)/room/chat-1"]]);
});

it("stays on Home for a push about any other room", async () => {
  lastResponse.mockReturnValue(response({ roomId: "call-1" }));
  await render(<NotificationTapRouter />);
  expect(navigate.mock.calls).toEqual([["/(app)/(tabs)/chats"]]);
  expect(push).not.toHaveBeenCalled();
});

it("waits for the room list before deciding, on a cold start", async () => {
  mockRoomsLoading = true;
  mockRooms = [];
  lastResponse.mockReturnValue(response({ roomId: "chat-1" }));
  const screen = await render(<NotificationTapRouter />);
  expect(push).not.toHaveBeenCalled();
  mockRoomsLoading = false;
  mockRooms = [CHAT];
  await screen.rerender(<NotificationTapRouter />);
  expect(push.mock.calls).toEqual([["/(app)/room/chat-1"]]);
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
