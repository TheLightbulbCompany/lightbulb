import { render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { FIXTURES, ROOM, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, agentNames: new Map() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live, useSuperAgent: () => true }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { MessageTasks } from "../MessageTasks";

it("the Router's one-line result carries no second task chip and no card: the ack's chip is the task's one (dev walk 13, W13-1)", async () => {
  const task = FIXTURES.step08.tasks[0];
  live.tasks = fakeTasksClient(FIXTURES.step08.tasks, FIXTURES.step08.details);
  const line = {
    messageId: "m9", roomId: ROOM, messageSeq: 9, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "Done: sent.", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-11-06T10:00:00Z", runId: null,
    kind: "task_line", taskRefs: [{ taskId: task.taskId, role: "started" }],
  } as CanonicalRoomMessage;
  const outputs = [{ id: "file:1", kind: "file" as const, title: "menu-blurbs.md", ref: {}, createdAt: "2026-11-06T10:00:00Z" }];
  live.tasks = fakeTasksClient([{ ...task, outputs }], FIXTURES.step08.details);
  const acked = new Set([`|${task.taskId}`]);
  const { unmount } = render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={vi.fn()} ackedTasks={acked} /></Fresh>);
  expect(await screen.findByRole("button", { name: "Open menu-blurbs.md" })).toBeInTheDocument();
  expect(document.querySelector("[data-task-chip]")).toBeNull();
  expect(document.querySelector("[data-task-card]")).toBeNull();
  unmount();
  // The ack in a thread (or not loaded): the finish line in the main list is the way in, so it keeps the chip.
  const { unmount: second } = render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={vi.fn()} ackedTasks={new Set([`m1|${task.taskId}`])} /></Fresh>);
  expect(await screen.findByRole("button", { name: new RegExp(task.title) })).toHaveAttribute("data-task-chip", task.taskId);
  second();
  // Nothing made (walk 15: the same chip twice under an Instagram post): beside its ack the chip is never repeated.
  live.tasks = fakeTasksClient([task], FIXTURES.step08.details);
  const { unmount: third } = render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={vi.fn()} ackedTasks={acked} /></Fresh>);
  await waitFor(() => expect(screen.queryByLabelText("Loading tasks")).toBeNull());
  expect(document.querySelector("[data-task-chip]")).toBeNull();
  third();
  // A failed task always keeps its chip, so Retry is reachable from the finish line.
  live.tasks = fakeTasksClient([{ ...task, status: "failed", shownGroup: "waiting_on_you", ownerUserId: SAM, outputs }], FIXTURES.step08.details);
  render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={vi.fn()} ackedTasks={acked} /></Fresh>);
  expect(await screen.findByRole("button", { name: /Didn't finish/ })).toHaveAttribute("data-task-chip", task.taskId);
});

it("the Router's ack carries its task as a compact chip with the specialist, never a full card (dev walk 10)", async () => {
  const task = FIXTURES.step08.tasks[0];
  live.tasks = fakeTasksClient(FIXTURES.step08.tasks, FIXTURES.step08.details);
  const ack = {
    messageId: "m8", roomId: ROOM, messageSeq: 8, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "Growth is researching three taglines.", mentions: [], replyToMessageId: null, threadRootMessageId: null,
    createdAt: "2026-11-06T10:00:00Z", runId: null, taskRefs: [{ taskId: task.taskId, role: "started" }],
  } as CanonicalRoomMessage;
  render(<Fresh><MessageTasks message={ack} viewerId={SAM} /></Fresh>);
  const chip = await screen.findByRole("button", { name: new RegExp(task.title) });
  expect(chip).toHaveAttribute("data-task-chip", task.taskId);
  if (task.specialist) expect(chip.textContent).toContain("·");
  expect(document.querySelector("[data-task-card]")).toBeNull();
});

it("one task, one live chip: Working with its timer, then Done with the run's length and what it made (dev walk 12, W12-1)", async () => {
  const base = FIXTURES.step08.tasks[0];
  const started = new Date(Date.now() - 38_000).toISOString();
  const working = { ...base, status: "working" as const, shownGroup: "working" as const, activity: { n: 1, status: "running" as const, startedAt: started } };
  live.tasks = fakeTasksClient([working], FIXTURES.step08.details);
  const msg = (kind?: "task_line") => ({
    messageId: kind ? "m9" : "m8", roomId: ROOM, messageSeq: 8, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: started, runId: null,
    kind, taskRefs: [{ taskId: base.taskId, role: "started" }],
  }) as CanonicalRoomMessage;
  const { unmount } = render(<Fresh><MessageTasks message={msg()} viewerId={SAM} /></Fresh>);
  expect(await screen.findByRole("button", { name: /Working · 0:3\d/ })).toHaveAttribute("data-mark", "working");
  unmount();

  const done = { ...working, status: "idle" as const, shownGroup: "idle" as const,
    activity: { ...working.activity, status: "ended" as const, endedAt: new Date(Date.parse(started) + 62_000).toISOString() },
    outputs: [{ id: "file:1", kind: "file" as const, title: "bakery-tagline-options.md", ref: {}, createdAt: started }] };
  live.tasks = fakeTasksClient([done], FIXTURES.step08.details);
  const acked = new Set([`|${base.taskId}`]);
  render(<Fresh><MessageTasks message={msg()} viewerId={SAM} ackedTasks={acked} /><MessageTasks message={msg("task_line")} viewerId={SAM} ackedTasks={acked} /></Fresh>);
  const chips = await screen.findAllByRole("button", { name: new RegExp(`${base.title}, Done · 1:02`) });
  // One chip for the task, the ack's (W13-1); what it made shows once, on the finish line.
  expect(chips).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Open bakery-tagline-options.md" })).toHaveLength(1);
});

it("one clock (W17): Done is the run's stored length on every read, just after it ended and long after (0:22 then 0:21 was two)", async () => {
  const base = FIXTURES.step08.tasks[0];
  const start = Date.now() - 17_600;
  const done = { ...base, status: "idle" as const, shownGroup: "idle" as const,
    activity: { n: 1, status: "ended" as const, startedAt: new Date(start).toISOString(), endedAt: new Date(start + 16_600).toISOString() } };
  live.tasks = fakeTasksClient([done], FIXTURES.step08.details);
  const ack = { messageId: "m8", roomId: ROOM, messageSeq: 8, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: done.activity.startedAt, runId: null,
    taskRefs: [{ taskId: base.taskId, role: "started" }] } as CanonicalRoomMessage;
  const { unmount } = render(<Fresh><MessageTasks message={ack} viewerId={SAM} /></Fresh>);
  expect(await screen.findByRole("button", { name: /Done · 0:16/ })).toBeInTheDocument();
  unmount();
  live.tasks = fakeTasksClient([{ ...done, activity: { ...done.activity, startedAt: new Date(start - 60_000).toISOString(), endedAt: new Date(start - 43_400).toISOString() } }], FIXTURES.step08.details);
  render(<Fresh><MessageTasks message={ack} viewerId={SAM} /></Fresh>);
  expect(await screen.findByRole("button", { name: /Done · 0:16/ })).toBeInTheDocument();
});

it("W17: a task asked a moment ago reads Working · 0:00 before its run starts, never a bare Working", async () => {
  const base = FIXTURES.step08.tasks[0];
  const asked = { ...base, status: "queued" as const, shownGroup: "working" as const, activity: null, createdAt: new Date().toISOString() };
  live.tasks = fakeTasksClient([asked], FIXTURES.step08.details);
  const ack = { messageId: "m8", roomId: ROOM, messageSeq: 8, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: asked.createdAt, runId: null,
    taskRefs: [{ taskId: base.taskId, role: "started" }] } as CanonicalRoomMessage;
  render(<Fresh><MessageTasks message={ack} viewerId={SAM} /></Fresh>);
  expect(await screen.findByRole("button", { name: /Working · 0:0\d/ })).toBeInTheDocument();
});

it("a task the viewer can't view shows no chip and no output under the ack or the finish line (dev walk 12, privacy)", async () => {
  const base = FIXTURES.step08.tasks[0];
  // The server leaves it out of the room list and answers its read with 404 (hidden reads as missing).
  const client = fakeTasksClient([]);
  client.get.mockRejectedValue(Object.assign(new Error("not_found"), { status: 404 }));
  live.tasks = client;
  const msg = (kind?: "task_line") => ({
    messageId: kind ? "m9" : "m8", roomId: ROOM, messageSeq: 8, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: kind ? "Done." : "On it.", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-11-06T10:00:00Z",
    runId: null, kind, taskRefs: [{ taskId: base.taskId, role: "started" }],
  }) as CanonicalRoomMessage;
  render(<Fresh><MessageTasks message={msg()} viewerId={SAM} /><MessageTasks message={msg("task_line")} viewerId={SAM} /></Fresh>);
  await waitFor(() => expect(client.get).toHaveBeenCalledWith(base.taskId));
  expect(document.querySelector("[data-task-chip]")).toBeNull();
  expect(screen.queryByRole("list", { name: "Made by this task" })).toBeNull();
  expect(screen.queryByText(base.title)).toBeNull();
});

it("a short result shows whole in its finish message with Copy, and no chip for the text it is (dev walk 15, W15-1)", async () => {
  const task = FIXTURES.step08.tasks[0];
  const outputs = [{ id: "text:1", kind: "document" as const, title: task.title, ref: { roomId: "t", messageId: "r1" }, createdAt: "2026-11-06T10:00:00Z" }];
  live.tasks = fakeTasksClient([{ ...task, outputs }], FIXTURES.step08.details);
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const line = {
    messageId: "m9", roomId: ROOM, messageSeq: 9, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "Drafted the Instagram post.\n\nTuesday is pizza night!\n\nSave your spot: [BOOKING LINK]", mentions: [], replyToMessageId: null,
    threadRootMessageId: null, createdAt: "2026-11-06T10:00:00Z", runId: null, kind: "task_line", taskRefs: [{ taskId: task.taskId, role: "started" }],
  } as CanonicalRoomMessage;
  render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={vi.fn()} ackedTasks={new Set([`|${task.taskId}`])} /></Fresh>);
  await waitFor(() => expect(screen.queryByLabelText("Loading tasks")).toBeNull());
  expect(document.querySelector("[data-task-chip]")).toBeNull();
  expect(screen.queryByRole("button", { name: `Open ${task.title}` })).toBeNull();
  screen.getByRole("button", { name: "Copy" }).click();
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("Tuesday is pizza night!\n\nSave your spot: [BOOKING LINK]"));
  expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
});
