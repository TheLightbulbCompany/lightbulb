import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, LIGHTBULB, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "@/components/tasks/__tests__/fakeTasks";

const live = vi.hoisted(() => ({ tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../MessageTaskCard", () => ({ MessageTaskCard: () => <div data-testid="legacy-task-card" /> }));
// The row as the server holds it: activate flips it, and the card's re-read sees the flip.
const automation = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
const api = vi.hoisted(() => ({
  get: vi.fn(async (path: string) => path === "/automations/a1" && automation.current ? automation.current : Promise.reject(new Error("404"))),
  post: vi.fn(async () => { automation.current = { ...automation.current, status: "active" }; return automation.current; }),
  del: vi.fn(async () => undefined),
}));
const AUTOMATION = vi.hoisted(() => ({
  id: "a1", name: "Post signups every Monday", instructions: "Post the signup count so far in #workshop-launch.", status: "draft", pausedReason: null,
  schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" }, nextRuns: [], hooks: [], specialistId: null, deliverTo: "room:room-workshop-launch",
  budgetPerRunUsd: 1, ownerUserId: "user_sam", createdBy: "router", mode: null, lastTaskId: null, activeRunTaskId: null, runCount: 0, version: 1,
}));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
const clerk = vi.hoisted(() => ({ userId: "user_sam", role: "org:admin" as string | undefined }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: clerk.userId }), useOrganization: () => ({ membership: clerk.role ? { role: clerk.role } : null }) }));

import { RoomMessageItem } from "../RoomMessageItem";
import type { RoomMessage } from "../types";

let client: FakeTasksClient;
beforeEach(() => {
  client = fakeTasksClient(FIXTURES.step07.tasks);
  live.tasks = client;
});

function item(message: RoomMessage, over: Partial<Parameters<typeof RoomMessageItem>[0]> = {}) {
  return <Fresh><RoomMessageItem id="m" message={message} text={message.text} currentUserId={SAM} actionable inThread={false} collapsed={false}
    replyCount={0} highlighted={false} mentionsViewer={false} markdown={{}} urlTransform={(u) => u} quickReactions={[]}
    reactionPending={false} reactionError={false} {...over} /></Fresh>;
}
const [ask, reply, result] = FIXTURES.step07.messages as RoomMessage[];

describe("RoomMessageItem tasks", () => {
  it("Lightbulb's own text answer in a DM offers Copy; a person's message and a channel answer don't (W18)", () => {
    const answer = { ...reply, text: "Sip, steep, and discover your new favorite brew.", taskRefs: [], kind: null, specialist: null } as RoomMessage;
    const { unmount } = render(item(answer, { copyable: true }));
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    unmount();
    const r2 = render(item(answer));
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
    r2.unmount();
    render(item({ ...answer, author: { kind: "human", id: SAM, displayName: "Sam" } } as RoomMessage, { copyable: true }));
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it("specialist byline", () => {
    render(item(result));
    expect(screen.getByText("Finance")).toBeInTheDocument();
    // The label says Lightbulb already: no "Agent" tag beside it (audit #28).
    expect(screen.queryByText("Agent")).toBeNull();
  });

  it("the front agent's own post carries no Agent tag; another agent's still does", () => {
    const front = { kind: "agent" as const, id: reply.author.id, displayName: "Lightbulb", mentionHandle: "lightbulb", role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 };
    const plain = { ...reply, specialist: null } as RoomMessage;
    const { unmount } = render(item(plain, { participant: front }));
    expect(screen.queryByText("Agent")).toBeNull();
    unmount();
    render(item(plain, { participant: { ...front, displayName: "Growth", mentionHandle: "growth" } }));
    expect(screen.getByText("Agent")).toBeInTheDocument();
  });

  it("one card per ref, no Suggested block", async () => {
    client.list.mockResolvedValue({ tasks: FIXTURES.step02.tasks, next: null });
    render(item(reply));
    expect(await screen.findByText("Workshop signup app")).toBeInTheDocument();
    expect(screen.getByText("Launch email draft")).toBeInTheDocument();
    expect(screen.getByText("Stripe fee estimate")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Suggested" })).toBeNull();
    expect(screen.queryByTestId("legacy-task-card")).toBeNull();
  });

  it("routed-to copy", async () => {
    const onOpenTask = vi.fn();
    const { unmount } = render(item({ ...ask, routedTo: { taskIds: ["t-signup"], started: 0, passThrough: true } }, { onOpenTask }));
    await userEvent.click(await screen.findByRole("button", { name: "Sent to Workshop signup app ›" }));
    expect(onOpenTask).toHaveBeenCalledWith("t-signup");
    unmount();
    render(item(ask));
    expect(screen.getByRole("button", { name: "3 tasks started ›" })).toBeInTheDocument();
  });

  it("agent 👀 chip is not toggleable", async () => {
    const onToggleReaction = vi.fn();
    render(item({ ...ask, routedTo: null, reactions: { "👀": [LIGHTBULB] } }, { onToggleReaction, isAgentId: (id) => id === LIGHTBULB }));
    const chip = screen.getByRole("img", { name: "Lightbulb saw this" });
    expect(chip).not.toHaveAttribute("aria-pressed");
    await userEvent.click(chip);
    expect(onToggleReaction).not.toHaveBeenCalled();
  });

  it("an automation_confirm card turns the automation on only through its button", async () => {
    automation.current = { ...AUTOMATION };
    const card = { ...reply, messageId: "m-confirm", text: "Set up below. Turn it on with the button on the card.", taskRefs: undefined, kind: "automation_confirm", automationId: "a1" } as RoomMessage;
    render(item(card));
    const region = await screen.findByRole("region", { name: "Turn on Post signups every Monday" });
    expect(region).toHaveTextContent("Mondays 9:00 AM");
    expect(region).toHaveTextContent("up to $1 a run, about $4 a month");
    await userEvent.click(within(region).getByRole("button", { name: "Turn on" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/activate", {});
    expect(await within(region).findByText("On")).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Turn on" })).toBeNull();
  });

  it("a member who may not turn a room automation on reads the waiting line, no buttons", async () => {
    clerk.role = "org:member";
    automation.current = { ...AUTOMATION };
    const card = { ...reply, messageId: "m-confirm", text: "Set up below. Turn it on with the button on the card.", taskRefs: undefined, kind: "automation_confirm", automationId: "a1" } as RoomMessage;
    render(item(card));
    const region = await screen.findByRole("region", { name: "Turn on Post signups every Monday" });
    expect(within(region).getByText("Waiting on a workspace admin")).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Turn on" })).toBeNull();
    // The owner may still drop the proposal (the server's _may_edit).
    expect(within(region).getByRole("button", { name: "Not now" })).toBeInTheDocument();
    clerk.role = "org:admin";
  });

  it("never prints raw cron, names one next run, and a paused automation is neutral with Turn on", async () => {
    // The A11 acceptance run leaves the row a paused one-off; any odd cron reads "Custom schedule".
    automation.current = { ...AUTOMATION, status: "paused", schedule: { cron: ["0 9 1 * *"], tz: "America/New_York" },
      nextRuns: ["2026-12-01T14:00:00Z", "2027-01-01T14:00:00Z"] };
    const card = { ...reply, messageId: "m-confirm", text: "Set up below. Turn it on with the button on the card.", taskRefs: undefined, kind: "automation_confirm", automationId: "a1" } as RoomMessage;
    render(item(card));
    const region = await screen.findByRole("region", { name: "Turn on Post signups every Monday" });
    expect(region).toHaveTextContent("Custom schedule");
    expect(region).not.toHaveTextContent("0 9 1 * *");
    expect(region).not.toHaveTextContent("2027");
    const paused = within(region).getByText("Paused");
    expect(paused).not.toHaveClass("text-ok");
    expect(within(region).queryByRole("button", { name: "Not now" })).toBeNull();
    await userEvent.click(within(region).getByRole("button", { name: "Turn on" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/activate", {});
    expect(await within(region).findByText("On")).toHaveClass("text-ok");
  });

});
