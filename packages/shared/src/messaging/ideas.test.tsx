// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import type { ApiMethods } from "../api/client";
import { createMessagingClient } from "./client";
import { ideaEffortLine, ideaProgress, ideaTeammates, myStartsIn, orderIdeas, plansPollMs, useIdeas, waitsForPlan, type Idea, type IdeasClient, type RunningIdea } from "./ideas";

const idea = (id: string, agentId: string, agentName = agentId): Idea => ({
  id, ownerRole: "growth", kind: "agency", businessTypes: ["any"], title: `T ${id}`, why: "w",
  deliverables: ["a", "b", "c"], ourTurnaround: "about 2 days", yourPart: "about 5 minutes", agentId, agentName,
});

describe("ideas client", () => {
  it("reads, starts and dismisses through escaped doors", async () => {
    const methods = { get: vi.fn(async () => ({})), post: vi.fn(async () => null) };
    const client = createMessagingClient(methods as unknown as ApiMethods);
    await client.listIdeas();
    await client.startIdea("crm setup");
    await client.dismissIdea("crm-setup");
    expect(methods.get).toHaveBeenCalledWith("/ideas");
    expect(methods.post.mock.calls).toEqual([["/ideas/crm%20setup/start", {}], ["/ideas/crm-setup/dismiss", {}]]);
  });
});

describe("ideas helpers", () => {
  it("puts the DM agent's ideas first and keeps the server order otherwise", () => {
    const list = [idea("a", "eng"), idea("b", "growth"), idea("c", "eng"), idea("d", "growth")];
    expect(orderIdeas(list, "growth").map((i) => i.id)).toEqual(["b", "d", "a", "c"]);
    expect(orderIdeas(list, null).map((i) => i.id)).toEqual(["a", "b", "c", "d"]);
  });

  it("lists each teammate once and writes the effort line without prices", () => {
    expect(ideaTeammates([idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("c", "eng", "Engineering")]))
      .toEqual([{ agentId: "eng", agentName: "Engineering" }, { agentId: "growth", agentName: "Growth" }]);
    expect(ideaEffortLine(idea("a", "x"))).toBe("Your team: about 2 days · You: about 5 minutes");
  });

  it("counts done over every task but cancelled ones", () => {
    expect(ideaProgress({ done: 2, todo: 3, cancelled: 4 })).toEqual({ done: 2, total: 5 });
    expect(ideaProgress({})).toBeNull();
    expect(ideaProgress(undefined)).toBeNull();
  });
});

describe("a DM's plans", () => {
  const NOW = Date.parse("2026-10-15T12:00:00Z");
  const start = (id: string, createdAt: string, plan?: RunningIdea["plan"], roomId = "dm", starterId = "me"): RunningIdea => ({
    ideaId: id, title: id, agentId: "g", agentName: "G", roomId, projectId: `p-${id}`, starterId, starterName: "Me", createdAt, plan,
  });

  it("lists every own start in the DM, newest first", () => {
    const running = [start("a", "2026-10-15T10:00:00Z"), start("b", "2026-10-15T11:00:00Z"),
      start("c", "2026-10-15T11:30:00Z", undefined, "other"), start("d", "2026-10-15T11:40:00Z", undefined, "dm", "them")];
    expect(myStartsIn({ running }, "dm", "me").map((s) => s.ideaId)).toEqual(["b", "a"]);
  });

  it("waits five minutes for a first plan, and ten after a revision until a new pending plan", () => {
    expect(waitsForPlan(start("a", "2026-10-15T11:57:00Z"), NOW)).toBe(true);
    expect(waitsForPlan(start("a", "2026-10-15T11:50:00Z"), NOW)).toBe(false);
    const revised = (decidedAt: string) => start("a", "2026-10-15T09:00:00Z", { approvalId: "p1", status: "revision_requested", title: "Plan: a", decidedAt });
    expect(waitsForPlan(revised("2026-10-15T11:55:00Z"), NOW)).toBe(true);
    expect(waitsForPlan(revised("2026-10-15T11:45:00Z"), NOW)).toBe(false);
    expect(waitsForPlan(start("a", "2026-10-15T11:59:00Z", { approvalId: "p2", status: "pending", title: "Plan: a" }), NOW)).toBe(false);
    expect(plansPollMs([start("a", "2026-10-15T09:00:00Z"), revised("2026-10-15T11:55:00Z")], NOW)).toBe(10_000);
    expect(plansPollMs([start("a", "2026-10-15T09:00:00Z")], NOW)).toBe(0);
  });
});

describe("useIdeas", () => {
  const wrapper = ({ children }: { children: ReactNode }) =>
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;

  it("dismisses optimistically and puts the idea back when the server refuses", async () => {
    let refuse = false;
    const client: IdeasClient = {
      listIdeas: vi.fn(async () => ({ ideas: [idea("a", "x"), idea("b", "x")], running: [] })),
      startIdea: vi.fn(async () => ({ roomId: "r", projectId: "p" })),
      dismissIdea: vi.fn(async () => { if (refuse) throw new Error("no"); }),
    };
    const { result } = renderHook(() => useIdeas(client, "u:o"), { wrapper });
    await waitFor(() => expect(result.current.ideas).toHaveLength(2));
    expect(result.current.anyDismissed).toBe(false);
    await act(() => result.current.dismiss("a"));
    expect(result.current.ideas.map((i) => i.id)).toEqual(["b"]);
    expect(result.current.anyDismissed).toBe(true);
    refuse = true;
    await act(async () => { await result.current.dismiss("b").catch(() => {}); });
    expect(result.current.ideas.map((i) => i.id)).toEqual(["b"]);
    expect(await act(() => result.current.start("b"))).toEqual({ roomId: "r", projectId: "p" });
  });
});
