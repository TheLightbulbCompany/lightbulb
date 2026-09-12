import { render, waitFor } from "@testing-library/react-native";

// `mock`-prefixed so jest's babel hoisting allows referencing it inside the
// jest.mock() factory below (out-of-scope vars are otherwise rejected).
const mockPost = jest.fn(
  async (_path: string, _body: Record<string, unknown>) => ({}),
);
jest.mock("@lightbulb/shared", () => ({
  createApiClient: () => ({ post: mockPost }),
  // Needed by the agent-chat route. Omitting it made that branch throw, which
  // went unnoticed while no test exercised a tap.
  sessionIdFromKey: (key?: string) => (key ? key.split(":")[1] : undefined),
}));
jest.mock("../../lib/adapter", () => ({ makeAdapter: () => ({}) }));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "tok", userId: "user_1" }),
}));
jest.mock("expo-constants", () => ({
  expoConfig: { extra: { eas: { projectId: "proj_1" } } },
}));
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
// jest-expo's native expo-crypto mock stubs randomUUID() to return undefined;
// override it so getDeviceId() yields a real string device id.
jest.mock("expo-crypto", () => ({
  randomUUID: () => "11111111-1111-4111-8111-111111111111",
}));

import { PushRegistration } from "./PushRegistration";

it("requests a token and POSTs it once", async () => {
  // RNTL 14's render() is async — await it before asserting effects.
  await render(
    <PushRegistration
      getCurrentTarget={() => ({
        agentId: null,
        sessionId: null,
        roomId: null,
        threadId: null,
      })}
    />,
  );
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  const [path, body] = mockPost.mock.calls[0];
  expect(path).toBe("/devices");
  expect(body.token).toBe("ExponentPushToken[test]");
  expect(body.platform).toBe("ios");
  expect(typeof body.deviceId).toBe("string");
});

// ---------------------------------------------------------------------------
// Tap routing. A Teams push deliberately carries no `agentId` (the backend
// omits it — that id is a Paperclip uuid and would open a gateway chat that
// does not exist), so before the teams branch existed a tap matched neither
// route and went NOWHERE: the app opened wherever it last was and "A task
// needs you" was unreachable.
// ---------------------------------------------------------------------------

import * as Notifications from "expo-notifications";
import { router } from "expo-router";

const mockPush = router.push as jest.Mock;
const lastResponse = Notifications.getLastNotificationResponseAsync as jest.Mock;

function responseWith(data: Record<string, unknown>) {
  return { notification: { request: { content: { data } } } };
}

/** Drive the warm-path listener: mount, then hand it a tapped response. */
async function routeTap(data: Record<string, unknown>) {
  await mountAndSettle();
  const listener = (
    Notifications.addNotificationResponseReceivedListener as jest.Mock
  ).mock.calls.at(-1)?.[0] as (r: unknown) => void;
  listener(responseWith(data));
}

async function mountAndSettle() {
  await render(
    <PushRegistration
      getCurrentTarget={() => ({
        agentId: null,
        sessionId: null,
        roomId: null,
        threadId: null,
      })}
    />,
  );
}

describe("notification tap routing", () => {
  beforeEach(() => {
    mockPush.mockClear();
    lastResponse.mockResolvedValue(null);
  });

  it("routes a Teams push to the issue it concerns", async () => {
    lastResponse.mockResolvedValue(
      responseWith({
        source: "teams",
        eventType: "issue.thread_interaction_created",
        issueId: "issue_abc",
        companyId: "company_1",
      }),
    );

    await mountAndSettle();

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith({
        pathname: "/(app)/teams/issue/[id]",
        params: { id: "issue_abc" },
      }),
    );
  });

  it("routes every Teams event type the same way, not just interactions", async () => {
    lastResponse.mockResolvedValue(
      responseWith({ source: "teams", eventType: "approval.created", issueId: "issue_xyz" }),
    );

    await mountAndSettle();

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith({
        pathname: "/(app)/teams/issue/[id]",
        params: { id: "issue_xyz" },
      }),
    );
  });

  // `source` is required as well as `issueId` so an unrelated payload that
  // happens to carry an issueId cannot silently claim the Teams route.
  it("ignores an issueId that did not come from Teams", async () => {
    lastResponse.mockResolvedValue(responseWith({ issueId: "issue_abc" }));

    await mountAndSettle();

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPush).not.toHaveBeenCalled();
  });

  // The pre-existing agent-chat route must be untouched by the new branch.
  it("still routes an agent push to its chat", async () => {
    lastResponse.mockResolvedValue(
      responseWith({ agentId: "agent_1", sessionKey: "sess:abc" }),
    );

    await mountAndSettle();

    await waitFor(() => expect(mockPush).toHaveBeenCalled());
    expect(String(mockPush.mock.calls[0][0])).toContain("/agent/agent_1");
  });

  it("routes a room push to the conversation", async () => {
    await routeTap({ roomId: "room-1", messageId: "m1", messageSeq: 2, threadRootMessageId: null });
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1");
  });

  it("routes a thread reply to the thread screen", async () => {
    await routeTap({ roomId: "room-1", messageId: "m2", messageSeq: 3, threadRootMessageId: "m1" });
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/thread/m1");
  });

  // The room branch sits between Teams and agent, so both neighbours are
  // re-asserted on the warm path the room tests use.
  it("still routes a Teams push to its issue", async () => {
    await routeTap({ source: "teams", issueId: "i1" });
    expect(mockPush).toHaveBeenCalledWith({
      pathname: "/(app)/teams/issue/[id]",
      params: { id: "i1" },
    });
  });

  it("does nothing when the app was not opened from a notification", async () => {
    lastResponse.mockResolvedValue(null);

    await mountAndSettle();

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPush).not.toHaveBeenCalled();
  });
});
