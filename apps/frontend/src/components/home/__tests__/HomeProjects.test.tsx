// Home (super-agent): the chats and the project spaces. Each card's counts are its room's share of the reads Activity
// makes (the complete Needs you read and the workspace list), so Home and Activity can't disagree.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { overviewGroups, type TaskSummary } from "@lightbulb/shared";
import { Fresh, fakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";
import type { DirectoryRoom } from "@/components/messaging/types";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, agentNames: new Map() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));
const ctl = vi.hoisted(() => ({ openPanel: vi.fn(), closePane: vi.fn(), openAgentDmWithDraft: vi.fn(), openChatPanelWithDraft: vi.fn(), activateMessagesPanel: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => ctl);
const ideas = vi.hoisted(() => ({ ideas: [] as unknown[] }));
vi.mock("@/hooks/useIdeas", () => ({ useIdeas: () => ideas }));
const messaging = vi.hoisted(() => ({ value: null as unknown }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => messaging.value,
}));

import { HomeProjects, projectRooms, roomCounts } from "../HomeProjects";

const task = (taskId: string, roomId: string, shownGroup: TaskSummary["shownGroup"]): TaskSummary => ({
  taskId, identifier: taskId, title: `Request ${taskId}`, status: shownGroup === "resolved" ? "resolved" : "working",
  shownGroup, audience: { kind: "room", roomId }, audienceKind: "room", originRoomId: roomId, taskRoomId: `tr-${taskId}`,
  watching: false, updatedAt: "2026-11-05T00:00:00Z", version: 1,
});
const TASKS = [
  task("a", "launch", "waiting_on_you"), task("b", "launch", "ready_for_review"), task("c", "launch", "working"), task("d", "launch", "working"),
  task("e", "site", "working"), task("f", "site", "resolved"), task("g", "dm-lb", "waiting_on_you"),
];
const needsRead = TASKS.filter((t) => t.shownGroup === "waiting_on_you" || t.shownGroup === "ready_for_review");

const FRONT = "agent-lb";
const room = (roomId: string, extra: Partial<DirectoryRoom>): DirectoryRoom => ({
  roomId, name: roomId, kind: "channel", roomRevision: 1, lastMessageSeq: 1, lastMessageAt: "2026-11-05T00:00:00Z", unreadCount: 0,
  updatedAt: "2026-11-05T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, participants: [], ...extra,
});
const ROOMS = [
  room("site", { name: "Website", lastMessageAt: "2026-11-01T00:00:00Z" }),
  room("launch", { name: "Spring launch", topic: "Ship the sale page", lastMessageAt: "2026-11-04T00:00:00Z",
    lastMessage: { messageId: "m", seq: 1, authorId: FRONT, authorKind: "agent", authorName: "Lightbulb", textPreview: "Draft is ready", hasAttachments: false, createdAt: "2026-11-04T00:00:00Z", threadRootMessageId: null } }),
  room("dm-lb", { kind: "dm", name: null, participants: [{ kind: "agent", id: FRONT, displayName: "Lightbulb" } as DirectoryRoom["participants"][number]] }),
  room("old", { name: "Old", archivedAt: "2026-10-01T00:00:00Z" }),
  room("feed", { name: "Feed", feed: true }),
  room("vendor", { name: "Lightbulb team", shared: true }),
];
const directory = (rooms: DirectoryRoom[], extra = {}) => ({ directoryProps: { rooms, ceoAgentId: FRONT, state: "ready", onCreate: vi.fn(async () => true), ...extra } });

beforeEach(() => {
  live.tasks = fakeTasksClient(TASKS);
  apiGet.mockImplementation(async () => ({ tasks: needsRead, next: null }));
  messaging.value = directory(ROOMS);
  ideas.ideas = [];
  Object.values(ctl).forEach((f) => f.mockClear());
});

describe("roomCounts", () => {
  it("per-room counts add up to the workspace's Needs you and Working", () => {
    const counts = roomCounts(needsRead, TASKS);
    expect(counts.get("launch")).toEqual({ working: 2, needsYou: 2 });
    expect(counts.get("site")).toEqual({ working: 1, needsYou: 0 });
    const sum = (k: "working" | "needsYou") => [...counts.values()].reduce((n, c) => n + c[k], 0);
    expect(sum("needsYou")).toBe(needsRead.length);
    expect(sum("working")).toBe(overviewGroups(TASKS).find((g) => g.id === "working")!.tasks.length);
  });
});

describe("projectRooms", () => {
  it("keeps channels and group chats, newest first; drops DMs, archived, the feed and the shared channel", () => {
    expect(projectRooms(ROOMS).map((r) => r.roomId)).toEqual(["launch", "site"]);
  });
});

describe("HomeProjects", () => {
  it("shows the slim needs-you line, the Lightbulb DM and project cards with their room's counts", async () => {
    render(<Fresh><HomeProjects /></Fresh>);
    const line = await screen.findByRole("button", { name: /3 need you · Review/ });
    await userEvent.click(line);
    expect(ctl.openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });

    const cards = screen.getByRole("region", { name: "Projects" });
    const items = within(cards).getAllByRole("listitem");
    expect(items.map((li) => within(li).getAllByText(/./)[0].textContent)).toEqual(["Spring launch", "Website"]);
    const launch = items[0];
    expect(launch).toHaveTextContent("Ship the sale page");
    expect(launch).toHaveTextContent("Lightbulb: Draft is ready");
    expect(await within(launch).findByText("Working 2")).toBeInTheDocument();
    expect(within(launch).getByText("2 need you")).toBeInTheDocument();
    expect(within(items[1]).getByText("Working 1")).toBeInTheDocument();
    expect(within(items[1]).queryByText(/need/)).toBeNull();

    const chats = screen.getByRole("region", { name: "Chats" });
    expect(within(chats).getByText("1 needs you")).toBeInTheDocument();
  });

  it("a card opens its room; the DM row and Ask open the Lightbulb DM", async () => {
    const opened = vi.fn();
    window.addEventListener("openRoom", (e) => opened((e as CustomEvent).detail), { once: true });
    render(<Fresh><HomeProjects /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: /^Spring launch/ }));
    expect(opened).toHaveBeenCalledWith({ roomId: "launch", threadId: null });
    await userEvent.click(within(screen.getByRole("region", { name: "Chats" })).getByRole("button"));
    expect(ctl.openAgentDmWithDraft).toHaveBeenLastCalledWith(FRONT, "");
    await userEvent.type(screen.getByLabelText("Ask Lightbulb"), "Plan the launch{Enter}");
    expect(ctl.openAgentDmWithDraft).toHaveBeenLastCalledWith(FRONT, "Plan the launch");
  });

  it("New project creates a channel and opens it", async () => {
    const value = directory(ROOMS);
    messaging.value = value;
    render(<Fresh><HomeProjects /></Fresh>);
    await userEvent.click(screen.getByRole("button", { name: "New project" }));
    await userEvent.type(screen.getByLabelText("Project name"), "Hiring{Enter}");
    expect(value.directoryProps.onCreate).toHaveBeenCalledWith({ kind: "channel", name: "Hiring", members: [] });
    expect(ctl.activateMessagesPanel).toHaveBeenCalled();
  });

  it("no needs-you line when nothing needs you", async () => {
    apiGet.mockImplementation(async () => ({ tasks: [], next: null }));
    render(<Fresh><HomeProjects /></Fresh>);
    await screen.findByRole("region", { name: "Projects" });
    expect(screen.queryByRole("button", { name: /need you · Review/ })).toBeNull();
  });

  it("a new workspace: the DM, Start a project and three starter ideas", async () => {
    messaging.value = directory([room("general", { name: "general", isDefault: true }), ROOMS[2]]);
    ideas.ideas = ["i1", "i2", "i3", "i4"].map((id) => ({ id, title: `Idea ${id}`, suggested: false, agentId: FRONT }));
    render(<Fresh><HomeProjects /></Fresh>);
    expect(screen.getByRole("region", { name: "Chats" })).toHaveTextContent("Lightbulb");
    expect(screen.getByRole("button", { name: "Start a project" })).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Starter ideas" })).getAllByRole("button")).toHaveLength(3);
  });
});
