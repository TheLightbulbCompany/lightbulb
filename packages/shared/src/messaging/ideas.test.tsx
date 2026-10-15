// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import type { ApiMethods } from "../api/client";
import { createMessagingClient } from "./client";
import { ideaSections, orderIdeas, useIdeas, type Idea, type IdeasClient } from "./ideas";

const idea = (id: string, agentId: string, agentName = agentId, ownerRole = "growth"): Idea => ({
  id, ownerRole, emoji: "💡", kind: "agency", size: "project", businessTypes: ["any"], title: `T ${id}`, why: "w",
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

  it("sorts ideas into sections by their role, suggestions first, keeping the order inside", () => {
    const list = [idea("e1", "x", "x", "engineering"), idea("f1", "x", "x", "finance"), idea("c1", "x", "x", "ceo"),
      { ...idea("s1", "x", "x", "growth"), suggested: true }, idea("o1", "x", "x", "customer_operations"),
      idea("f2", "x", "x", "finance")];
    expect(ideaSections(list).map((s) => [s.title, s.ideas.map((i) => i.id)])).toEqual([
      ["For you", ["s1"]], ["Money", ["f1", "f2"]], ["Run the day", ["c1", "o1"]], ["Build", ["e1"]],
    ]);
  });
});

describe("useIdeas", () => {
  const wrapper = ({ children }: { children: ReactNode }) =>
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;

  it("dismisses optimistically and puts the idea back when the server refuses", async () => {
    let refuse = false;
    const client: IdeasClient = {
      listIdeas: vi.fn(async () => ({ ideas: [idea("a", "x"), idea("b", "x")] })),
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
