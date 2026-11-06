import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { FIXTURES, ROOM, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, agentNames: new Map() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live, useSuperAgent: () => true }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { MessageTasks } from "../MessageTasks";

it("the Router's one-line result carries its task as a chip that opens it, not a second card (UX slice 3)", async () => {
  const task = FIXTURES.step08.tasks[0];
  live.tasks = fakeTasksClient(FIXTURES.step08.tasks, FIXTURES.step08.details);
  const line = {
    messageId: "m9", roomId: ROOM, messageSeq: 9, roomRevision: 1, author: { kind: "agent", id: "ceo", displayName: "Lightbulb" },
    text: "Done: sent.", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-11-06T10:00:00Z", runId: null,
    kind: "task_line", taskRefs: [{ taskId: task.taskId, role: "started" }],
  } as CanonicalRoomMessage;
  const onOpen = vi.fn();
  render(<Fresh><MessageTasks message={line} viewerId={SAM} onOpen={onOpen} /></Fresh>);
  const chip = await screen.findByRole("button", { name: new RegExp(task.title) });
  expect(chip).toHaveAttribute("data-task-chip", task.taskId);
  expect(document.querySelector("[data-task-card]")).toBeNull();
  await userEvent.click(chip);
  expect(onOpen).toHaveBeenCalledWith(task.taskId);
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
  render(<Fresh><MessageTasks message={msg()} viewerId={SAM} /><MessageTasks message={msg("task_line")} viewerId={SAM} /></Fresh>);
  const chips = await screen.findAllByRole("button", { name: new RegExp(`${base.title}, Done · 1:02`) });
  expect(chips).toHaveLength(2);
  expect(screen.getAllByRole("button", { name: "Open bakery-tagline-options.md" })).toHaveLength(2);
});
