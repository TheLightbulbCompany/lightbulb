import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, LIGHTBULB, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../MessageTaskCard", () => ({ MessageTaskCard: () => <div data-testid="legacy-task-card" /> }));

import { RoomMessageItem } from "../RoomMessageItem";
import type { RoomMessage } from "../types";

let client: FakeTasksClient;
beforeEach(() => {
  live.superAgent = true;
  client = fakeTasksClient(FIXTURES.step07.tasks);
  live.tasks = client;
});

function item(message: RoomMessage, over: Partial<Parameters<typeof RoomMessageItem>[0]> = {}) {
  return <Fresh><RoomMessageItem id="m" message={message} text={message.text} currentUserId={SAM} actionable inThread={false} collapsed={false}
    replyCount={0} highlighted={false} mentionsViewer={false} markdown={{}} urlTransform={(u) => u} quickReactions={[]}
    reactionPending={false} reactionError={false} {...over} /></Fresh>;
}
const [ask, reply, result] = FIXTURES.step07.messages as RoomMessage[];

describe("RoomMessageItem with the super-agent switch", () => {
  it("specialist byline", () => {
    render(item(result));
    expect(screen.getByText("Lightbulb · Finance")).toBeInTheDocument();
    expect(screen.getByText("Agent")).toBeInTheDocument();
  });

  it("started and suggested refs split", async () => {
    client.list.mockResolvedValue({ tasks: FIXTURES.step02.tasks, next: null });
    render(item(reply));
    expect(await screen.findByText("Workshop signup app")).toBeInTheDocument();
    expect(screen.getByText("Launch email draft")).toBeInTheDocument();
    const suggested = screen.getByRole("region", { name: "Suggested" });
    expect(suggested).toHaveTextContent("Stripe fee estimate");
    expect(suggested).toHaveTextContent("would use a paid tool");
    expect(screen.queryByTestId("legacy-task-card")).toBeNull();
  });

  it("routed-to copy", async () => {
    const onOpenTask = vi.fn();
    const { unmount } = render(item({ ...ask, routedTo: { taskIds: ["t-signup"], started: 0, suggested: 0, passThrough: true } }, { onOpenTask }));
    await userEvent.click(await screen.findByRole("button", { name: "Sent to Workshop signup app ›" }));
    expect(onOpenTask).toHaveBeenCalledWith("t-signup");
    unmount();
    render(item(ask));
    expect(screen.getByRole("button", { name: "Sent to 2 threads, 1 suggested ›" })).toBeInTheDocument();
  });

  it("agent 👀 chip is not toggleable", async () => {
    const onToggleReaction = vi.fn();
    render(item({ ...ask, routedTo: null, reactions: { "👀": [LIGHTBULB] } }, { onToggleReaction, isAgentId: (id) => id === LIGHTBULB }));
    const chip = screen.getByRole("img", { name: "Lightbulb saw this" });
    expect(chip).not.toHaveAttribute("aria-pressed");
    await userEvent.click(chip);
    expect(onToggleReaction).not.toHaveBeenCalled();
  });

  it("flag off is identical to today", async () => {
    live.superAgent = false;
    const withAttrs = { ...reply, specialist: "finance", reactions: { "👀": [LIGHTBULB] } };
    const props = { onToggleReaction: vi.fn(), isAgentId: (id: string) => id === LIGHTBULB, onOpenTask: vi.fn() };
    const off = render(item(withAttrs, props));
    const norm = (h: string) => h.replace(/radix-[^"]+/g, "radix");
    const html = norm(off.container.innerHTML);
    expect(screen.getByTestId("legacy-task-card")).toBeInTheDocument();
    off.unmount();
    const today = render(item({ ...withAttrs, taskRefs: undefined, specialist: undefined, routedTo: undefined }, props));
    expect(norm(today.container.innerHTML)).toBe(html);
    expect(client.list).not.toHaveBeenCalled();
  });
});
