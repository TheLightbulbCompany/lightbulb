import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { fixtureTask } from "@lightbulb/shared/tasks/fixtures";

const rooms = vi.hoisted(() => ({
  activeRoomId: "dm1" as string | null,
  messages: [] as unknown[],
  optimisticMessages: [] as unknown[],
  runs: [] as { roomId: string; state: string }[],
  activeRoom: null as { kind: string; name: string | null } | null,
  updateRoom: vi.fn(),
  selectRoom: vi.fn(),
  sendMessage: vi.fn(),
}));
vi.mock("@/hooks/useRooms", () => ({ useRooms: () => rooms }));
const live = vi.hoisted(() => ({
  tasks: { cacheScope: "org_t", get: vi.fn() },
  messaging: { attachmentUrl: vi.fn() },
  agentNames: new Map<string, string>(),
}));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
// The card is a lazy chunk: load it before the first card mounts.
beforeAll(() => import("../openui/OpenUICard"));
const fresh = (ui: React.ReactElement) => render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>);
const agent = { kind: "agent", id: "a1", displayName: "Lightbulb" };

import { AssistantThread } from "../AssistantThread";

beforeEach(() => {
  rooms.activeRoomId = "dm1";
  rooms.selectRoom.mockReset();
  rooms.sendMessage.mockReset().mockResolvedValue(undefined);
  rooms.messages = [
    { messageId: "m1", text: "Book Sam for **Friday**", author: { kind: "human", id: "u1", displayName: "Ana" } },
    { messageId: "m2", text: "Done.", author: { kind: "agent", id: "a1", displayName: "Lightbulb" } },
  ];
  rooms.optimisticMessages = [{ clientMessageId: "c1", text: "Thanks" }];
  rooms.runs = [];
  rooms.activeRoom = { kind: "dm", name: null };
  rooms.updateRoom.mockReset().mockResolvedValue(undefined);
});

it("a New chat's first message names it; a DM, or a chat with history, is never renamed", async () => {
  rooms.activeRoom = { kind: "group-dm", name: null };
  rooms.messages = [];
  const { unmount } = render(<AssistantThread roomId="dm1" />);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "Plan the spring promo" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(rooms.updateRoom).toHaveBeenCalledWith({ name: "Plan the spring promo" }));
  unmount();
  rooms.updateRoom.mockClear();
  rooms.activeRoom = { kind: "dm", name: null };
  render(<AssistantThread roomId="dm1" />);
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Hi" } });
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalled());
  expect(rooms.updateRoom).not.toHaveBeenCalled();
});

it("shows one replying row while a run in this room is running, and none otherwise", () => {
  rooms.runs = [{ roomId: "other", state: "running" }, { roomId: "dm1", state: "completed" }];
  const { rerender } = render(<AssistantThread roomId="dm1" />);
  expect(screen.queryByText("Lightbulb is replying…")).toBeNull();
  rooms.runs = [{ roomId: "dm1", state: "running" }];
  rerender(<AssistantThread roomId="dm1" />);
  expect(screen.getByText("Lightbulb is replying…")).toBeInTheDocument();
});

it("a failed send stays as its own Not sent row while the next is typed and sent; Retry reuses its id", async () => {
  rooms.messages = [];
  rooms.optimisticMessages = [];
  let failA!: (e: Error) => void;
  rooms.sendMessage.mockImplementationOnce(() => new Promise((_, reject) => { failA = reject; }));
  render(<AssistantThread roomId="dm1" />);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "Cancel Tuesday" } });
  fireEvent.keyDown(box, { key: "Enter" });
  fireEvent.change(box, { target: { value: "And book Friday" } }); // B is being typed when A fails
  await act(async () => failA(new Error("timeout")));
  expect(box).toHaveValue("And book Friday"); // the draft is the person's, never overwritten
  const row = screen.getByText("Cancel Tuesday").closest("li")!;
  expect(row).toHaveAttribute("data-failed");
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalledTimes(2));
  expect(screen.getByText("Cancel Tuesday").closest("li")).toHaveAttribute("data-failed"); // A survives B's send
  const idA = rooms.sendMessage.mock.calls[0][0].clientMessageId;
  expect(rooms.sendMessage.mock.calls[1][0].clientMessageId).not.toBe(idA);
  fireEvent.click(within(row).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalledTimes(3));
  expect(rooms.sendMessage.mock.calls[2][0]).toEqual(expect.objectContaining({ clientMessageId: idA, text: "Cancel Tuesday" }));
  await waitFor(() => expect(screen.queryByText("Cancel Tuesday")).toBeNull());
});

it("opens the DM and renders its messages, markdown and all", () => {
  render(<AssistantThread roomId="dm1" />);
  expect(rooms.selectRoom).toHaveBeenCalledWith("dm1");
  expect(screen.getByText("Friday").tagName).toBe("STRONG");
  expect(screen.getByText("Done.")).toBeInTheDocument();
  expect(screen.getByText("Lightbulb")).toBeInTheDocument();
  expect(screen.getByText("Thanks").closest("li")).toHaveAttribute("data-pending");
});

it("waits until the hook has the DM open", () => {
  rooms.activeRoomId = "other";
  render(<AssistantThread roomId="dm1" />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
});

it("Enter sends the text through the hook and empties the box; Shift+Enter does not", () => {
  render(<AssistantThread roomId="dm1" />);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "  Move it to 3pm  " } });
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(rooms.sendMessage).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" });
  expect(rooms.sendMessage).toHaveBeenCalledWith(expect.objectContaining({ text: "Move it to 3pm", mentions: [], replyToMessageId: null, threadRootMessageId: null }));
  expect(box).toHaveValue("");
});

it("(a) an agent's openui card draws live buttons; a press replies to the card's message, addressed to its author", async () => {
  rooms.messages = [{ messageId: "card1", roomId: "dm1", threadRootMessageId: null, author: agent,
    text: "Approve this?\n\n```openui-lang\nroot = Stack([b])\nb = Buttons([Button(\"Approve\"), Button(\"Deny\")])\n```" }];
  rooms.optimisticMessages = [];
  fresh(<AssistantThread roomId="dm1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalledWith(expect.objectContaining({
    text: "Approve", replyToMessageId: "card1", threadRootMessageId: null, mentions: [{ kind: "agent", id: "a1" }],
  })));
  expect(screen.queryByText(/openui-lang/)).toBeNull();
});

it("(b) a task result shows its status, opens its file in a new tab, and offers the full answer", async () => {
  const task = fixtureTask({ taskId: "t1", title: "Spring menu", status: "idle", shownGroup: "idle",
    activity: { n: 1, status: "ended", outcome: "done", lastHeartbeatAt: "2026-11-04T14:00:00.000Z" },
    outputs: [{ id: "o1", kind: "file", title: "menu.pdf", ref: { roomId: "task-room-t1", attachmentId: "att1" }, createdAt: "2026-11-04T14:00:00.000Z" }] });
  live.tasks.get.mockResolvedValue({ task, attempts: [{ n: 1, status: "ended", startedAt: "2026-11-04T14:00:00.000Z", output: "The whole long answer." }], questions: [], effects: [], subscriptions: [], taskRoomId: "task-room-t1" });
  live.messaging.attachmentUrl.mockResolvedValue({ url: "https://files.example/menu.pdf" });
  const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
  const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
  rooms.messages = [{ messageId: "r1", roomId: "dm1", threadRootMessageId: null, author: agent, kind: "task_line", text: "Done: the menu.", taskRefs: [{ taskId: "t1", role: "started" }] }];
  rooms.optimisticMessages = [];
  fresh(<AssistantThread roomId="dm1" />);
  fireEvent.click(await screen.findByRole("button", { name: "menu.pdf" }));
  await waitFor(() => expect(tab.location.href).toBe("https://files.example/menu.pdf"));
  expect(live.messaging.attachmentUrl).toHaveBeenCalledWith("task-room-t1", "att1");
  expect(tab.opener).toBeNull();
  expect(screen.getByText("Full answer")).toBeInTheDocument();
  expect(screen.getByText("The whole long answer.")).toBeInTheDocument();
  open.mockRestore();
});

it("(c) an automation_confirm message keeps its Turn on button", async () => {
  api.get.mockImplementation(async (path: string) => {
    if (path === "/automations/au1") return { id: "au1", name: "Weekly promo", instructions: "", status: "draft", pausedReason: null,
      schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" }, nextRuns: [], hooks: [], specialistId: null, deliverTo: "dm",
      budgetPerRunUsd: null, ownerUserId: "user_test_123", createdBy: "router", mode: null, lastTaskId: null, activeRunTaskId: null, runCount: 0, version: 1 };
    if (path === "/messaging/rooms") return { rooms: [] };
    throw new Error(`no route ${path}`);
  });
  api.post.mockResolvedValue({});
  rooms.messages = [{ messageId: "m9", roomId: "dm1", threadRootMessageId: null, author: agent, kind: "automation_confirm", automationId: "au1",
    text: "Set up below. Turn it on with the button on the card." }];
  rooms.optimisticMessages = [];
  fresh(<AssistantThread roomId="dm1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/automations/au1/activate", {}));
});

it("a message's attachments show, an attachment-only one included, and open in a new tab", async () => {
  live.messaging.attachmentUrl.mockResolvedValue({ url: "https://files.example/photo.jpg" });
  const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
  const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
  rooms.messages = [{ messageId: "p1", roomId: "dm1", threadRootMessageId: null, author: { kind: "human", id: "u1", displayName: "Ana" }, text: "",
    attachments: [{ id: "att9", fileName: "photo.jpg", contentType: "image/jpeg", size: 10 }] }];
  rooms.optimisticMessages = [];
  fresh(<AssistantThread roomId="dm1" />);
  fireEvent.click(screen.getByRole("button", { name: "photo.jpg" }));
  await waitFor(() => expect(tab.location.href).toBe("https://files.example/photo.jpg"));
  expect(live.messaging.attachmentUrl).toHaveBeenCalledWith("dm1", "att9");
  open.mockRestore();
});
