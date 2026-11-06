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
