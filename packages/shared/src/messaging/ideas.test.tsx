// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import type { ApiMethods } from "../api/client";
import { createMessagingClient } from "./client";
import { ideaEffortLine, ideaProgress, orderIdeas, runningLine, useIdeas, type Idea, type IdeasClient } from "./ideas";

const idea = (id: string, agentId: string, agentName = agentId): Idea => ({
  id, ownerRole: "growth", kind: "agency", size: "project", businessTypes: ["any"], title: `T ${id}`, why: "w",
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

  it("writes a running idea's line: a project's tasks done, or a task's status", () => {
    expect(runningLine({ size: "project", progress: { done: 2, todo: 3 } })).toBe("2 of 5 tasks done");
    expect(runningLine({ size: "project" })).toBe("Planning");
    expect(runningLine({ size: "task", progress: { in_progress: 1 } })).toBe("In progress");
    expect(runningLine({ size: "task" })).toBe("To do");
  });

  it("has no effort line for a suggestion without one", () => {
    expect(ideaEffortLine({})).toBeNull();
  });

  it("writes the effort line without prices", () => {
    expect(ideaEffortLine(idea("a", "x"))).toBe("Your team: about 2 days · You: about 5 minutes");
  });

  it("counts done over every task but cancelled ones", () => {
    expect(ideaProgress({ done: 2, todo: 3, cancelled: 4 })).toEqual({ done: 2, total: 5 });
    expect(ideaProgress({})).toBeNull();
    expect(ideaProgress(undefined)).toBeNull();
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
