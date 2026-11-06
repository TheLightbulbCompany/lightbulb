// First run (UX slice 6): the first run's ideas wait in the Lightbulb DM as cards; the list is re-read when the first
// run's task changes; nothing is read with the switch off.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import type { Idea, LiveEvent } from "@lightbulb/shared";

const client = vi.hoisted(() => ({ listIdeas: vi.fn(), startIdea: vi.fn(), dismissIdea: vi.fn() }));
const handlers = vi.hoisted(() => new Set<(event: unknown) => void>());
const live = vi.hoisted(() => ({
  superAgent: true, agentNames: new Map([["ceo", "Lightbulb"]]), messaging: null as unknown,
  onEvent: (h: (event: unknown) => void) => { handlers.add(h); return () => handlers.delete(h); },
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => ({ openMessagePanel: vi.fn(), openTaskPane: vi.fn(), openPanel: vi.fn(), openChatPanelWithDraft: vi.fn() }));

import { FirstMoves } from "../FirstMoves";

const idea = (id: string, over: Partial<Idea> = {}): Idea => ({
  id, kind: "everyday", size: "task", businessTypes: ["any"], emoji: "💡", title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [], agentId: "growth", agentName: "Growth", suggested: true, ...over,
});
const dm = () => render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><FirstMoves /></SWRConfig>);

beforeEach(() => {
  vi.clearAllMocks();
  handlers.clear();
  live.superAgent = true;
  live.messaging = client;
  client.dismissIdea.mockResolvedValue(undefined);
});

describe("FirstMoves", () => {
  it("shows only the first run's ideas, each with Not now and Do it", async () => {
    client.listIdeas.mockResolvedValue({ ideas: [idea("a", { firstRun: true }), idea("b", { firstRun: true }), idea("c", { firstRun: true }), idea("other")], firstRunTaskId: "t-first" });
    dm();
    const region = await screen.findByRole("region", { name: "First moves" });
    expect(region).toHaveTextContent("Three first moves for you. Pick one and I'll start it:");
    expect(within(region).getAllByRole("listitem")).toHaveLength(3);
    expect(within(region).queryByText("Title other")).toBeNull();
    const card = within(region).getByRole("heading", { name: "Title a" }).closest("li")!;
    await userEvent.click(within(card).getByRole("button", { name: "Not now" }));
    expect(client.dismissIdea).toHaveBeenCalledWith("a");
    await waitFor(() => expect(within(region).getAllByRole("listitem")).toHaveLength(2));
  });

  it("re-reads the list when the first run's task changes, not on any other task", async () => {
    client.listIdeas.mockResolvedValueOnce({ ideas: [], firstRunTaskId: "t-first" });
    dm();
    await waitFor(() => expect(client.listIdeas).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("region", { name: "First moves" })).toBeNull();
    client.listIdeas.mockResolvedValue({ ideas: [idea("a", { firstRun: true })], firstRunTaskId: "t-first" });
    const fire = (taskId: string) => act(() => { handlers.forEach((h) => h({ type: "task.changed", payload: { taskId, part: "status", version: 2 } } as LiveEvent)); });
    fire("t-other");
    expect(client.listIdeas).toHaveBeenCalledTimes(1);
    fire("t-first");
    expect(await screen.findByRole("region", { name: "First moves" })).toHaveTextContent("One first move left");
  });

  it("switch off: no read at all", () => {
    live.superAgent = false;
    dm();
    expect(client.listIdeas).not.toHaveBeenCalled();
  });
});
