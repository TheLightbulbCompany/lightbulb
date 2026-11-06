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
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => ({ items: [] }) }));
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));
const ctl = vi.hoisted(() => ({ openPanel: vi.fn(), closePane: vi.fn(), openAgentDmWithDraft: vi.fn(), activateMessagesPanel: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => ctl);
const ideas = vi.hoisted(() => ({ ideas: [] as unknown[] }));
vi.mock("@/hooks/useIdeas", () => ({ useIdeas: () => ideas }));
const firstRun = vi.hoisted(() => ({ moves: [] as unknown[], open: vi.fn() }));
vi.mock("@/components/ideas/FirstMoves", () => ({ FIRST_MOVE_LABEL: "Pick a first move", useFirstMoves: () => firstRun.moves, openFirstMoves: firstRun.open }));
const messaging = vi.hoisted(() => ({ value: null as unknown }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => messaging.value,
}));

import { HomeProjects, cardLabel, chatRooms, projectRooms, roomCounts, workingCapped } from "../HomeProjects";

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
const directory = (rooms: DirectoryRoom[], extra = {}) => ({ directoryProps: { rooms, ceoAgentId: FRONT, state: "ready", onCreate: vi.fn(async () => true), ...extra },
  askLightbulb: vi.fn(async (_text: string) => {}) });

beforeEach(() => {
  live.tasks = fakeTasksClient(TASKS);
  apiGet.mockImplementation(async () => ({ tasks: needsRead, next: null }));
  messaging.value = directory(ROOMS);
  ideas.ideas = [];
  firstRun.moves = [];
  firstRun.open.mockClear();
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

describe("Working's page cap", () => {
  it("a floor (N+) only when the first page is all live and more follow", () => {
    expect(workingCapped({ tasks: TASKS, next: "p2" })).toBe(false); // a done row on the page: every live one is in
    expect(workingCapped({ tasks: TASKS.filter((t) => t.shownGroup !== "resolved"), next: "p2" })).toBe(true);
    expect(workingCapped({ tasks: TASKS.filter((t) => t.shownGroup !== "resolved"), next: null })).toBe(false);
    expect(cardLabel("Launch", { working: 50, needsYou: 1 }, true)).toBe("Launch, Working 50+, 1 needs you");
  });
});

const priya = { kind: "human", id: "u-priya", displayName: "Priya" } as DirectoryRoom["participants"][number];
const lb = { kind: "agent", id: FRONT, displayName: "Lightbulb" } as DirectoryRoom["participants"][number];
const CHATS = [
  room("group", { kind: "group-dm", name: "walk group", participants: [lb, priya], lastMessageAt: "2026-11-06T00:00:00Z" }),
  room("dm-priya", { kind: "dm", name: null, participants: [priya] }),
  room("dm-growth", { kind: "dm", name: null, participants: [{ ...lb, id: "agent-growth", displayName: "Growth" }] }),
];

describe("projectRooms", () => {
  it("keeps channels only, newest first; drops DMs, group chats, archived, the feed and the shared channel", () => {
    expect(projectRooms([...ROOMS, ...CHATS]).map((r) => r.roomId)).toEqual(["launch", "site"]);
  });
  it("W17-4: group chats and DMs with people are chats, newest first; the Lightbulb and specialist DMs are not", () => {
    expect(chatRooms([...ROOMS, ...CHATS], FRONT).map((r) => r.roomId)).toEqual(["group", "dm-priya"]);
  });
});

describe("HomeProjects", () => {
  it("shows the slim needs-you line, the Lightbulb DM and channel cards with their room's counts", async () => {
    render(<Fresh><HomeProjects /></Fresh>);
    const line = await screen.findByRole("button", { name: /3 need you · Review/ });
    await userEvent.click(line);
    expect(ctl.openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });

    const cards = screen.getByRole("region", { name: "Channels" });
    const items = within(cards).getAllByRole("listitem");
    expect(await within(items[0]).findByRole("button", { name: "Spring launch, Working 2, 2 need you" })).toBeInTheDocument();
    // UX slice 6: tile · name · one state line. Needs you wins over working; no topic, no message preview.
    const launch = items[0];
    expect(launch).not.toHaveTextContent("Ship the sale page");
    expect(launch).not.toHaveTextContent("Draft is ready");
    expect(within(launch).getByText("2 need you")).toBeInTheDocument();
    expect(within(launch).queryByText(/working/)).toBeNull();
    expect(within(items[1]).getByText("1 working")).toBeInTheDocument();
    expect(within(items[1]).queryByText(/need/)).toBeNull();

    const chats = screen.getByRole("region", { name: "Chats" });
    expect(within(chats).getByText("1 needs you")).toBeInTheDocument();
  });

  it("W17-4: a group chat is listed under Chats with faces, never as a # channel card", async () => {
    messaging.value = directory([...ROOMS, ...CHATS]);
    const { container } = render(<Fresh><HomeProjects /></Fresh>);
    const chats = screen.getByRole("region", { name: "Chats" });
    const group = within(chats).getByRole("button", { name: /walk group/ });
    expect(group.querySelector(".room-directory-faces")).not.toBeNull();
    expect(group.querySelector(".lucide-hash")).toBeNull();
    expect(within(chats).getByRole("button", { name: /Priya/ })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Channels" })).queryByText("walk group")).toBeNull();
    expect(container.querySelector(".home-projects__dm .lucide-hash")).toBeNull();
  });

  it("a card opens its room; the DM row opens the Lightbulb DM; Ask sends into it", async () => {
    const value = directory(ROOMS);
    messaging.value = value;
    const opened = vi.fn();
    window.addEventListener("openRoom", (e) => opened((e as CustomEvent).detail), { once: true });
    render(<Fresh><HomeProjects /></Fresh>);
    const card = await screen.findByRole("button", { name: "Spring launch, Working 2, 2 need you" });
    await userEvent.click(card);
    expect(opened).toHaveBeenCalledWith({ roomId: "launch", threadId: null });
    await userEvent.click(within(screen.getByRole("region", { name: "Chats" })).getByRole("button", { name: /Lightbulb/ }));
    expect(ctl.openAgentDmWithDraft).toHaveBeenLastCalledWith(FRONT, "");
    await userEvent.type(screen.getByLabelText("Ask Lightbulb"), "Plan the launch{Enter}");
    expect(value.askLightbulb).toHaveBeenCalledWith("Plan the launch");
    expect(screen.getByLabelText("Ask Lightbulb")).toHaveValue("");
  });

  it("a failed Ask keeps the text and says why", async () => {
    const value = directory(ROOMS);
    value.askLightbulb.mockRejectedValueOnce(new Error("Lightbulb isn't ready yet."));
    messaging.value = value;
    render(<Fresh><HomeProjects /></Fresh>);
    await userEvent.type(screen.getByLabelText("Ask Lightbulb"), "Hi{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Lightbulb isn't ready yet.");
    expect(screen.getByLabelText("Ask Lightbulb")).toHaveValue("Hi");
  });

  it("New channel: a dialog with the name, its tile and a goal; it creates the channel and opens it", async () => {
    const value = directory(ROOMS);
    messaging.value = value;
    render(<Fresh><HomeProjects /></Fresh>);
    // Frame A (UX slice 4): the page's one primary action sits in the title row, beside "Home".
    const title = screen.getByRole("heading", { level: 1, name: "Home" }).parentElement!;
    expect(title).toHaveClass("frame-doc__title");
    await userEvent.click(within(title).getByRole("button", { name: "New channel" }));
    const dialog = await screen.findByRole("dialog", { name: "New channel" });
    expect(within(dialog).getByRole("button", { name: "Create channel" })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Channel icon and colour" }));
    await userEvent.click(await screen.findByRole("button", { name: "Rocket" }));
    await userEvent.click(screen.getByRole("button", { name: "Sand" }));
    await userEvent.type(within(dialog).getByLabelText(/What should this channel get done/), "Hire two bakers");
    await userEvent.type(within(dialog).getByLabelText("Name"), "Hiring{Enter}");
    expect(value.directoryProps.onCreate).toHaveBeenCalledWith({ kind: "channel", name: "Hiring", members: [], topic: "Hire two bakers", icon: "rocket", color: "project-3" });
    expect(ctl.activateMessagesPanel).toHaveBeenCalled();
  });

  it("Pinned channels sit above the rest; a card's ⋯ pins, opens Settings and archives", async () => {
    const onSetPrefs = vi.fn(async () => {});
    const onOpenSettings = vi.fn();
    const onPatchRoom = vi.fn(async () => {});
    const mine = [{ kind: "human", id: "me", role: "owner" }] as DirectoryRoom["members"];
    messaging.value = directory([{ ...ROOMS[0], starred: true, members: mine }, { ...ROOMS[1], members: mine }, ...ROOMS.slice(2)],
      { onSetPrefs, onOpenSettings, onPatchRoom, currentUserId: "me" });
    render(<Fresh><HomeProjects /></Fresh>);
    const pinned = screen.getByRole("region", { name: "Pinned" });
    expect(within(pinned).getAllByRole("listitem")).toHaveLength(1);
    expect(within(pinned).getByRole("button", { name: /^Website/ })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Channels" })).queryByRole("button", { name: /^Website/ })).toBeNull();
    await userEvent.click(within(pinned).getByRole("button", { name: "More for Website" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Unpin" }));
    expect(onSetPrefs).toHaveBeenCalledWith("site", { starred: false });
    await userEvent.click(within(pinned).getByRole("button", { name: "More for Website" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Settings" }));
    expect(onOpenSettings).toHaveBeenCalledWith("site", "home");
    await userEvent.click(within(pinned).getByRole("button", { name: "More for Website" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Archive" }));
    expect(onPatchRoom).toHaveBeenCalledWith("site", { archived: true });
  });

  it("first run: the one needs-you item reads Pick a first move and opens the DM; Start a channel offers the moves", async () => {
    apiGet.mockImplementation(async () => ({ tasks: [], next: null }));
    firstRun.moves = [{ id: "s-1", title: "Launch a pre-order page", firstRun: true }];
    messaging.value = directory([room("general", { name: "general", isDefault: true }), ROOMS[2]]);
    render(<Fresh><HomeProjects /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: /1 needs you · Pick a first move/ }));
    expect(firstRun.open).toHaveBeenCalledTimes(1);
    expect(ctl.openPanel).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("list", { name: "First moves" })).getByRole("button", { name: "Launch a pre-order page" }));
    expect(firstRun.open).toHaveBeenCalledTimes(2);
  });

  it("no needs-you line when nothing needs you", async () => {
    apiGet.mockImplementation(async () => ({ tasks: [], next: null }));
    render(<Fresh><HomeProjects /></Fresh>);
    await screen.findByRole("region", { name: "Channels" });
    expect(screen.queryByRole("button", { name: /need you · Review/ })).toBeNull();
  });

  it("a new workspace: the DM, Start a channel and three starter ideas", async () => {
    messaging.value = directory([room("general", { name: "general", isDefault: true }), ROOMS[2]]);
    ideas.ideas = ["i1", "i2", "i3", "i4"].map((id) => ({ id, title: `Idea ${id}`, suggested: false, agentId: FRONT }));
    render(<Fresh><HomeProjects /></Fresh>);
    expect(screen.getByRole("region", { name: "Chats" })).toHaveTextContent("Lightbulb");
    expect(screen.getByRole("button", { name: "Start a channel" })).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Starter ideas" })).getAllByRole("button")).toHaveLength(3);
  });
});
