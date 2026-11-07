import { ApiError, type ActivityAction, type ActivityItem } from "@lightbulb/shared";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { Alert, StyleSheet } from "react-native";

import { chatPalette } from "../../../../config/theme";

// Every out-of-scope name a jest.mock factory reaches is `mock`-prefixed and
// read lazily (babel lowers these to `var`; the factories run mid-import).
const mockPush = jest.fn();
let mockFocused = true;
let mockParams: Record<string, string> = {};
jest.mock("expo-router", () => ({
  router: { push: (...args: unknown[]) => mockPush(...args) },
  useIsFocused: () => mockFocused,
  useLocalSearchParams: () => mockParams,
}));

const mockRunAction = jest.fn();
const mockMarkSeen = jest.fn(async () => {});
const mockLoadMore = jest.fn();
const mockRefresh = jest.fn(async () => {});
let mockState: Record<string, unknown> = {};
jest.mock("../../../../lib/activity", () => ({
  ...jest.requireActual("../../../../lib/activity"),
  useMobileActivity: () => ({
    items: [], unseenCount: 0, paperclipAvailable: true, loading: false, loadingMore: false,
    error: undefined, hasMore: false,
    loadMore: mockLoadMore, refresh: mockRefresh, markSeen: mockMarkSeen, runAction: mockRunAction,
    ...mockState,
  }),
}));
// Stable, as the real one is memoized: a fresh object per render re-runs the roster load forever.
const mockConfig = {};
jest.mock("../../../../lib/teamsData", () => ({
  useTeamsConfig: () => mockConfig,
  fetchAgents: async () => [{ id: "agent-9", name: "Growth" }],
  fetchMembers: async () => [{ userId: "user_2", name: "Dana" }],
}));
jest.mock("../../../../lib/workspaceRoster", () => ({
  useWorkspaceRoster: () => ({
    agents: [{ id: "agent-9", name: "Growth", status: "running" }],
    members: [{ userId: "user_2", name: "Dana" }],
  }),
}));

import Activity from "../activity";

const action = (over: Partial<ActivityAction> & { id: string; label: string }): ActivityAction => ({
  style: null, confirm: null, input: null, navigate: null, ...over,
});
const item = (over: Partial<ActivityItem> = {}): ActivityItem => ({
  id: "paperclip:approval:1", kind: "paperclip", subtype: "approval", roomId: null, messageId: null,
  threadRootMessageId: null, actor: { kind: "agent", id: "agent-1", name: "Growth" },
  createdAt: new Date(Date.now() - 2 * 60_000).toISOString(), state: "open",
  resolvedBy: null, resolvedAt: null, read: false,
  summary: {
    title: "Growth wants to publish a post on LinkedIn",
    requester: { kind: "agent", id: "agent-1", name: "Growth" },
    reason: "Launch announcement",
    preview: { text: "We just shipped v2", destination: "LinkedIn · Lightbulb page", fields: [] },
    links: [],
  },
  actions: [
    action({ id: "approve", label: "Approve", style: "primary" }),
    action({ id: "reject", label: "Reject", style: "destructive", confirm: "Reject this request?",
      input: { kind: "text", label: "Why? (optional)", required: false, options: null } }),
    action({ id: "open", label: "Open issue", navigate: { kind: "issue", id: "iss-1", roomId: null } }),
  ],
  ...over,
});

beforeEach(() => {
  jest.clearAllMocks();
  mockFocused = true;
  mockParams = {};
  mockState = {};
  mockRunAction.mockImplementation(async (id: string) => item({ id, state: "resolved", actions: [] }));
});

it("pins a needs-you item under its header with the why, time, unread dot and a Review button", async () => {
  mockState = { items: [item()] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  expect(screen.getByRole("header", { name: /Needs you/ })).toBeTruthy();
  expect(screen.getByText("Growth")).toBeTruthy();
  expect(screen.getByText("wants to publish a post on LinkedIn")).toBeTruthy();
  expect(screen.getByText("Launch announcement")).toBeTruthy();
  expect(screen.getByText("2m")).toBeTruthy();
  expect(screen.getByLabelText("Unread")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  expect(alert).toHaveBeenCalledWith(
    "Growth wants to publish a post on LinkedIn",
    "Launch announcement\nWe just shipped v2\nTo LinkedIn · Lightbulb page",
    expect.any(Array),
  );
});

it("filters with chips and groups the rest by day", async () => {
  mockState = {
    items: [
      item(),
      item({ id: "mention:m1", kind: "mention", subtype: null, roomId: "room-1", read: true,
        summary: { ...item().summary, title: "Bob mentioned you in #general" }, actions: [] }),
    ],
  };
  await render(<Activity />);
  expect(screen.getByText("Today")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Mentions" }));
  expect(screen.queryByRole("header", { name: /Needs you/ })).toBeNull();
  expect(screen.getByText("Bob mentioned you in #general")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Threads" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Agents’ work" })).toBeNull();
});

it("opens on a pushed approval, highlighted, under All", async () => {
  mockParams = { focus: "paperclip:approval:1" };
  mockState = { items: [item()] };
  await render(<Activity />);
  expect(screen.getByRole("button", { name: "All" }).props.accessibilityState.selected).toBe(true);
  const row = screen.getByLabelText("Growth wants to publish a post on LinkedIn");
  expect(row).toBeTruthy();
  // The visible row itself (not just its now-covered wrapper) carries the tint.
  expect(StyleSheet.flatten(row.props.style).backgroundColor).toBe(chatPalette.light.selected);
});

it("offers an ordinary row's actions on long press", async () => {
  const alert = jest.spyOn(Alert, "alert");
  mockState = {
    items: [item({ id: "mention:m1", kind: "mention", subtype: null, roomId: "room-1",
      summary: { ...item().summary, title: "Bob mentioned you in #general" },
      actions: [action({ id: "mark_done", label: "Mark done" })] })],
  };
  await render(<Activity />);
  expect(screen.queryByRole("button", { name: "Mark done" })).toBeNull();
  await fireEvent(screen.getByLabelText("Bob mentioned you in #general"), "longPress");
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Mark done")!.onPress!(); });
  expect(mockRunAction).toHaveBeenCalledWith("mention:m1", "mark_done", {});
});

it("shows who resolved an item and no buttons", async () => {
  mockState = {
    items: [item({
      read: true, state: "resolved", actions: [],
      resolvedBy: { kind: "human", id: "user_2", name: "Dana" },
      resolvedAt: new Date(Date.now() - 5 * 60_000).toISOString(),
    })],
  };
  await render(<Activity />);
  expect(screen.getByText("Resolved by Dana · 5m")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(screen.queryByLabelText("Unread")).toBeNull();
});

it("runs a plain action from the Review sheet", async () => {
  mockState = { items: [item()] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Approve")!.onPress!(); });
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:approval:1", "approve", {});
});

it("confirms, then asks for the note, then runs with it", async () => {
  mockState = { items: [item()] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const reviewButtons = alert.mock.calls[0][2]!;
  await act(async () => { reviewButtons.find((b) => b.text === "Reject")!.onPress!(); });
  expect(alert).toHaveBeenNthCalledWith(2, "Reject this request?", undefined, expect.any(Array));
  expect(mockRunAction).not.toHaveBeenCalled();
  const buttons = alert.mock.calls[1][2]!;
  await act(async () => { buttons.find((b) => b.text === "Reject")!.onPress!(); });
  await fireEvent.changeText(screen.getByLabelText("Why? (optional)"), "off-brand");
  // The sheet's own submit, the only "Reject" button left on screen.
  await fireEvent.press(screen.getAllByRole("button", { name: "Reject" }).at(-1)!);
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:approval:1", "reject", { note: "off-brand", value: "off-brand" });
});

it("picks a select option and sends its value", async () => {
  mockState = {
    items: [item({ actions: [action({ id: "change_status", label: "Change status",
      input: { kind: "select", label: "Status", required: true, options: [{ value: "done", label: "Done" }] } })] })],
  };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Change status")!.onPress!(); });
  await fireEvent.press(screen.getByLabelText("Done"));
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:approval:1", "change_status", { value: "done" });
});

it("picks a person from the workspace roster", async () => {
  mockState = {
    items: [item({ actions: [action({ id: "reassign", label: "Reassign",
      input: { kind: "person", label: "Assign to", required: true, options: null } })] })],
  };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Reassign")!.onPress!(); });
  await fireEvent.press(await screen.findByLabelText("Dana"));
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:approval:1", "reassign", { value: "user_2" });
});

it("shows the server's message when an action fails", async () => {
  mockState = { items: [item()] };
  mockRunAction.mockRejectedValue(new Error("You can't decide this approval."));
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Approve")!.onPress!(); });
  expect(await screen.findByText("You can't decide this approval.")).toBeTruthy();
});

it("shows local neutral copy, not the server's, for a purchase-gate action failure", async () => {
  mockState = { items: [item()] };
  const detail = { blocked: true, code: "subscription_required", message: "Pick a plan to keep your agent." };
  mockRunAction.mockRejectedValue(new ApiError(402, { detail }, detail.message));
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Approve")!.onPress!(); });
  expect(await screen.findByText("This account doesn't have an active plan right now.")).toBeTruthy();
  expect(screen.queryByText(/pick a plan/i)).toBeNull();
});

it("navigates a thread row; old issue and run targets go nowhere", async () => {
  mockState = {
    items: [
      item(),
      item({ id: "mention:m1", kind: "mention", subtype: null, roomId: "room-1", threadRootMessageId: "root-1",
        summary: { ...item().summary, title: "Bob mentioned you in #general" }, actions: [] }),
      item({ id: "run:r1", kind: "agent_run", subtype: null, summary: { ...item().summary, title: "A run failed" },
        actions: [action({ id: "view", label: "View result", navigate: { kind: "run", id: "r1", roomId: null } })] }),
    ],
  };
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Growth wants to publish a post on LinkedIn"));
  expect(mockPush).not.toHaveBeenCalled();
  expect(mockRunAction).not.toHaveBeenCalled();

  mockPush.mockClear();
  await fireEvent.press(screen.getByLabelText("Bob mentioned you in #general"));
  expect(mockPush.mock.calls).toEqual([["/(app)/room/room-1"], ["/(app)/room/room-1/thread/root-1"]]);

  mockPush.mockClear();
  await fireEvent.press(screen.getByLabelText("A run failed"));
  expect(mockPush).not.toHaveBeenCalled();
});

it("opens a mention's thread from its Open thread action", async () => {
  mockState = {
    items: [
      item({ id: "mention:m2", kind: "mention", subtype: null, roomId: "room-1", read: true,
        summary: { ...item().summary, title: "Ann mentioned you in #general" },
        actions: [action({ id: "open", label: "Open thread", navigate: { kind: "thread", id: "m2", roomId: "room-1" } })] }),
    ],
  };
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Ann mentioned you in #general"));
  expect(mockPush.mock.calls).toEqual([["/(app)/room/room-1"], ["/(app)/room/room-1/thread/m2"]]);
});

it("pins an agent's question and confirms it inline", async () => {
  mockState = {
    items: [
      item({ id: "paperclip:interaction:iss-4:int-1", subtype: "interaction",
        summary: { ...item().summary, title: "Growth needs your confirmation: Launch email", reason: "Send it?", preview: null },
        actions: [
          action({ id: "accept", label: "Confirm", style: "primary" }),
          action({ id: "open", label: "Open task", navigate: { kind: "issue", id: "iss-4", roomId: null } }),
        ] }),
    ],
  };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  expect(screen.getByRole("header", { name: /Needs you/ })).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Review: Growth needs your confirmation: Launch email"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Confirm")!.onPress!(); });
  expect(mockRunAction).toHaveBeenCalledWith("paperclip:interaction:iss-4:int-1", "accept", {});
});

it("falls back to the Review sheet when the row has review actions but no navigation target", async () => {
  mockState = {
    items: [item({ roomId: null, threadRootMessageId: null,
      actions: [action({ id: "approve", label: "Approve", style: "primary" })] })],
  };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Growth wants to publish a post on LinkedIn"));
  expect(alert).toHaveBeenCalledWith(
    "Growth wants to publish a post on LinkedIn",
    "Launch announcement\nWe just shipped v2\nTo LinkedIn · Lightbulb page",
    expect.any(Array),
  );
});

it("suppresses reopening the Review sheet (press, fallback press, long-press) while its action is running", async () => {
  let resolveRun!: (value: unknown) => void;
  mockRunAction.mockImplementation(() => new Promise((resolve) => { resolveRun = resolve; }));
  mockState = {
    items: [item({ roomId: null, threadRootMessageId: null,
      actions: [action({ id: "approve", label: "Approve", style: "primary" })] })],
  };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  // Opens via the row's press fallback (no target on this item).
  await fireEvent.press(screen.getByLabelText("Growth wants to publish a post on LinkedIn"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Approve")!.onPress!(); });
  expect(mockRunAction).toHaveBeenCalledTimes(1);
  alert.mockClear();

  // Busy: the same row can no longer reopen the sheet, by press or long-press.
  await fireEvent.press(screen.getByLabelText("Growth wants to publish a post on LinkedIn"));
  await fireEvent(screen.getByLabelText("Growth wants to publish a post on LinkedIn"), "longPress");
  expect(alert).not.toHaveBeenCalled();
  expect(mockRunAction).toHaveBeenCalledTimes(1);

  await act(async () => { resolveRun(item({ id: "paperclip:approval:1", state: "resolved", actions: [] })); });
});

it("is not pressable with no review actions and no navigation target", async () => {
  mockState = { items: [item({ roomId: null, threadRootMessageId: null, actions: [] })] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Growth wants to publish a post on LinkedIn"));
  expect(alert).not.toHaveBeenCalled();
  expect(mockPush).not.toHaveBeenCalled();
});

// A message carries a Reply action, and counting that made every direct message
// ask for a review, in the button kept for what waits on the reader.
it("offers Open on a message, with its Reply one long press away", async () => {
  const alert = jest.spyOn(Alert, "alert");
  mockState = {
    items: [item({
      id: "dm:m9", kind: "dm", subtype: null, roomId: "room-9", messageId: "m9",
      actor: { kind: "human", id: "user-9", name: "Aden Kim" },
      summary: {
        ...item().summary, title: "Aden Kim sent you a direct message",
        requester: { kind: "human", id: "user-9", name: "Aden Kim" }, reason: null,
      },
      actions: [
        action({ id: "reply", label: "Reply", style: "primary" }),
        action({ id: "open", label: "Open conversation", navigate: { kind: "room", id: "room-9", roomId: "room-9" } }),
      ],
    })],
  };
  await render(<Activity />);
  expect(screen.getByLabelText("Open: Aden Kim sent you a direct message")).toBeTruthy();
  expect(screen.queryByText("Review")).toBeNull();
  await fireEvent(screen.getByLabelText("Aden Kim sent you a direct message"), "longPress");
  expect(alert).toHaveBeenCalledTimes(1);
  expect((alert.mock.calls[0][2] ?? []).map((button) => button.text)).toEqual(["Reply", "Open", "Cancel"]);
  alert.mockRestore();
});

it("offers Open, not Review, when the only actions navigate", async () => {
  mockState = { items: [item({ actions: [action({ id: "open", label: "Open task", navigate: { kind: "task", id: "t-1", roomId: null } })] })] };
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Open: Growth wants to publish a post on LinkedIn"));
  expect(mockPush).toHaveBeenCalledWith({ pathname: "/(app)/task/[taskId]", params: { taskId: "t-1" } });
  expect(screen.queryByText("Review")).toBeNull();
});

it("rings an agent actor that is running", async () => {
  mockState = { items: [item({ actor: { kind: "agent", id: "agent-9", name: "Growth" } })] };
  await render(<Activity />);
  expect(screen.getByLabelText("Working")).toBeTruthy();
});

it("marks seen up to the newest item while focused, and not while blurred", async () => {
  const newest = new Date(Date.now() - 60_000).toISOString();
  mockState = { items: [item({ createdAt: new Date(Date.now() - 3_600_000).toISOString() }), item({ id: "b", createdAt: newest })] };
  mockFocused = false;
  const view = await render(<Activity />);
  expect(mockMarkSeen).not.toHaveBeenCalled();
  mockFocused = true;
  await view.rerender(<Activity />);
  expect(mockMarkSeen).toHaveBeenCalledWith(newest);
});

it("shows the empty state, the Paperclip footnote and skeletons", async () => {
  mockState = { paperclipAvailable: false };
  const view = await render(<Activity />);
  expect(screen.getByText("You’re all caught up.")).toBeTruthy();
  expect(screen.getByText(/Paperclip is unavailable/)).toBeTruthy();

  mockState = { loading: true };
  await view.rerender(<Activity />);
  expect(screen.queryByText("You’re all caught up.")).toBeNull();
  expect(screen.getByTestId("activity-skeleton")).toBeTruthy();
});

it("reports a load failure in plain words", async () => {
  mockState = { error: new Error("Network request failed") };
  await render(<Activity />);
  expect(screen.getByText("Couldn’t load activity: Network request failed")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Retry" }));
  expect(mockRefresh).toHaveBeenCalled();
});

describe("activity links", () => {
  // A newer server may send a link kind this build predates: tapping must not crash.
  it("gives no screens for an unknown link kind", () => {
    const { activityHrefs } = jest.requireActual("../../../../lib/activity");
    expect(activityHrefs({ kind: "someday", id: "x" })).toEqual([]);
  });

  it("a task link (task_question item) opens its task screen", () => {
    const { activityHrefs } = jest.requireActual("../../../../lib/activity");
    expect(activityHrefs({ kind: "task", id: "t-1", roomId: null })).toEqual([{ pathname: "/(app)/task/[taskId]", params: { taskId: "t-1" } }]);
  });

  it("shows only the newest finished item per coding session", async () => {
    // As the backend sends them: every finished turn is its own OPEN item.
    const turn = (id: string, reason: string) => item({
      id, kind: "session", subtype: "finished", state: "open", actions: [],
      actor: { kind: "agent", id: "claude", name: "Claude" },
      summary: { title: "Claude · Fix the header", requester: { kind: "agent", id: "claude", name: "Claude" }, reason, preview: null,
        links: [{ kind: "session", id: "s1", label: "Session" }] },
    });
    mockState = { items: [turn("a", "Finished: newest"), turn("b", "Finished: older")] };
    await render(<Activity />);
    expect(await screen.findByText("Finished: newest")).toBeTruthy();
    expect(screen.queryByText("Finished: older")).toBeNull();
  });

  // A teammate's ask to join waits on the owner. The session finishing a turn
  // after it must not hide it.
  it("keeps an ask to join on the list when the session has a newer item", async () => {
    const about = (id: string, subtype: "finished" | "join_request", reason: string) => item({
      id, kind: "session", subtype, state: "open", actions: [],
      actor: { kind: "agent", id: "claude", name: "Claude" },
      summary: { title: "Claude · Fix the header", requester: { kind: "agent", id: "claude", name: "Claude" }, reason, preview: null,
        links: [{ kind: "session", id: "s1", label: "Session" }] },
    });
    mockState = { items: [about("a", "finished", "Finished: newest"), about("b", "join_request", "Bob asked to join")] };
    await render(<Activity />);
    expect(await screen.findByText("Finished: newest")).toBeTruthy();
    expect(screen.getByText("Bob asked to join")).toBeTruthy();
  });

  it("titles a coding-session item by the session, with the tool's mark", async () => {
    mockState = { items: [item({
      id: "session:s1:finished", kind: "session", subtype: null, state: "done", actions: [],
      actor: { kind: "agent", id: "claude", name: "Claude" },
      summary: { title: "Claude · Fix the header", requester: { kind: "agent", id: "claude", name: "Claude" }, reason: "Finished: all green", preview: null, links: [] },
    })] };
    await render(<Activity />);
    expect(await screen.findByText("Fix the header")).toBeTruthy();
    expect(screen.queryByText(/^· /)).toBeNull();
  });

});

const loginItem = (hint?: string) => item({
  id: "login:0123456789abcdef0123456789abcdef", kind: "login", subtype: "login",
  summary: { ...item().summary, title: "Growth needs the login for accounts.google.com", reason: null, preview: null },
  actions: [
    action({ id: "provide", label: "Add login", style: "primary",
      input: { kind: "login", label: "Login for accounts.google.com", required: true, options: null, hint } }),
    action({ id: "decline", label: "Not now" }),
  ],
});

it("answers a login ask with the web's value shape and forgets the password", async () => {
  mockState = { items: [loginItem("Google is connected. This password is only for the browser.")] };
  const alert = jest.spyOn(Alert, "alert");
  await render(<Activity />);
  await fireEvent.press(screen.getByLabelText("Review: Growth needs the login for accounts.google.com"));
  const buttons = alert.mock.calls[0][2]!;
  await act(async () => { buttons.find((b) => b.text === "Add login")!.onPress!(); });
  // A sheet, not the old "add this on the web" alert.
  expect(alert).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Google is connected. This password is only for the browser.")).toBeTruthy();
  const password = screen.getByLabelText("Password");
  expect(password.props.secureTextEntry).toBe(true);
  expect(password.props.textContentType).toBe("password");
  const username = screen.getByLabelText("Username or email");
  expect(username.props).toMatchObject({ autoCapitalize: "none", autoCorrect: false, textContentType: "username" });
  // Both fields are required.
  await fireEvent.changeText(username, "  dana@example.com ");
  expect(screen.getAllByRole("button", { name: "Add login" }).at(-1)!.props.accessibilityState?.disabled).toBe(true);
  await fireEvent.changeText(password, " p@ss word ");
  await fireEvent.press(screen.getAllByRole("button", { name: "Add login" }).at(-1)!);
  expect(mockRunAction).toHaveBeenCalledWith("login:0123456789abcdef0123456789abcdef", "provide", {
    value: JSON.stringify({ username: "dana@example.com", password: " p@ss word " }),
  });
  expect(screen.queryByDisplayValue(" p@ss word ")).toBeNull();
});

it("opens the login form straight from a focused sign-in link", async () => {
  mockParams = { focus: "login:0123456789abcdef0123456789abcdef" };
  mockState = { items: [loginItem()] };
  await render(<Activity />);
  expect(await screen.findByLabelText("Password")).toBeTruthy();
  expect(mockRunAction).not.toHaveBeenCalled();
});

it("does not open a form for a focused login ask that is already resolved", async () => {
  mockParams = { focus: "login:0123456789abcdef0123456789abcdef" };
  mockState = { items: [{ ...loginItem(), state: "resolved" }] };
  await render(<Activity />);
  expect(screen.queryByLabelText("Password")).toBeNull();
});
