import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

// Task 5 moved useAgentSessions' fetch from a single-shot useGatewayRpc call
// onto a paging useSWR fetcher built on useGatewayRpcMutation, so the whole
// gateway surface these tests need to stub is callRpc (sessions.list for the
// fetch, sessions.patch/sessions.delete for the mutations) — useGatewayRpc
// itself is no longer called by the hook.
const callRpc = vi.fn().mockResolvedValue({});
const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyApiError: (...args: unknown[]): unknown => notifyApiError(...args),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: vi.fn(),
  useGatewayRpcMutation: (): typeof callRpc => callRpc,
}));

interface SessionRow {
  key: string;
  label?: string;
  derivedTitle?: string;
  lastMessagePreview?: string;
  updatedAt?: number;
  totalTokens?: number;
  pinned?: boolean;
  model?: string;
  modelProvider?: string;
  thinkingLevel?: string;
  fastMode?: boolean;
}

// Stubs the single page the hook's sessions.list walk sees (hasMore: false
// stops the walk after one page). Every other RPC method resolves to {}
// unless a test overrides callRpc's implementation for its own scenario.
function stubSessionsList(rows: SessionRow[]): void {
  callRpc.mockImplementation(async (method: string) =>
    method === "sessions.list" ? { sessions: rows, hasMore: false } : {},
  );
}

// SWR's cache is module-global; every test renders with a fresh Map provider
// so an earlier test's cached page (same agentId cache key, or a dedupe
// window that suppresses a "new" fetch) never bleeds into a later one.
function renderAgentSessions(agentId: string | null, userId: string | null, archived = false) {
  return renderHook(() => useAgentSessions(agentId, userId, archived), {
    wrapper: ({ children }: { children: React.ReactNode }) => (
      <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
    ),
  });
}

import { useAgentSessions } from "../useAgentSessions";

describe("useAgentSessions — filtered per-agent 4-part rows", () => {
  beforeEach(() => {
    callRpc.mockReset().mockResolvedValue({});
    notifyApiError.mockClear();
  });

  it("returns only this agent's user-owned 4-part sessions with a label", async () => {
    stubSessionsList([
      { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", derivedTitle: "Taxes", updatedAt: 200, totalTokens: 5 },
      { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222", updatedAt: 300 },
      // legacy 3-part row — surfaced as the null row, sorted by recency
      { key: "agent:agent-X:user-1", updatedAt: 100 },
      // other agent — excluded
      { key: "agent:agent-Y:user-1:33333333-3333-4333-8333-333333333333" },
      // other member — excluded
      { key: "agent:agent-X:user-2:44444444-4444-4444-8444-444444444444" },
      // malformed trailing segment (not a UUID) — excluded
      { key: "agent:agent-X:user-1:not-a-uuid" },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");

    await waitFor(() => expect(result.current.sessions).toHaveLength(3));
    // Newest first, legacy row included in the ordering rather than pinned.
    expect(result.current.sessions.map((s) => s.sessionId)).toEqual([
      "22222222-2222-4222-8222-222222222222",
      "11111111-1111-4111-8111-111111111111",
      null,
    ]);
    expect(result.current.sessions[1].label).toBe("Taxes");
    // Untitled falls back to a short id label, never blank.
    expect(result.current.sessions[0].label).not.toBe("");
  });

  it("carries per-session model/effort overrides through on 4-part and General rows", async () => {
    stubSessionsList([
      {
        key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111",
        model: "openai/gpt-5",
        modelProvider: "openai",
        thinkingLevel: "high",
        fastMode: true,
        updatedAt: 200,
      },
      // legacy General carries its OWN overrides, not the 4-part row's
      { key: "agent:agent-X:user-1", model: "bedrock/claude", thinkingLevel: "low", updatedAt: 100 },
    ]);
    const { result } = renderAgentSessions("agent-X", "user-1");

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    const general = result.current.sessions.find((s) => s.sessionId === null);
    const named = result.current.sessions.find(
      (s) => s.sessionId === "11111111-1111-4111-8111-111111111111",
    );

    expect(named).toMatchObject({
      model: "openai/gpt-5",
      modelProvider: "openai",
      thinkingLevel: "high",
      fastMode: true,
    });
    expect(general).toMatchObject({ model: "bedrock/claude", thinkingLevel: "low" });
    // fastMode absent on General stays undefined (== inherit), never coerced.
    expect(general?.fastMode).toBeUndefined();
  });

  it("gives each agent its own scoped fetch (the server does the narrowing)", async () => {
    stubSessionsList([
      { key: "agent:friday:user_1:0b477873-1111-4111-8111-111111111111", label: "F" },
      { key: "agent:atlas:user_1:0b477873-2222-4222-8222-222222222222", label: "A" },
    ]);
    const { result } = renderHook(
      () => [useAgentSessions("friday", "user_1"), useAgentSessions("atlas", "user_1")] as const,
      {
        wrapper: ({ children }: { children: React.ReactNode }) => (
          <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
        ),
      },
    );
    await waitFor(() => expect(result.current[0].sessions).toHaveLength(1));
    await waitFor(() => expect(result.current[1].sessions).toHaveLength(1));
    expect(result.current[0].sessions[0].label).toBe("F");
    expect(result.current[1].sessions[0].label).toBe("A");
    const scoped = callRpc.mock.calls
      .filter(([m]) => m === "sessions.list")
      .map(([, p]) => (p as { agentId?: string }).agentId);
    expect(new Set(scoped)).toEqual(new Set(["friday", "atlas"]));
  });

  it("re-fetches for a different user — the key is per user, not per agent alone", async () => {
    stubSessionsList([{ key: "agent:friday:user_1:0b477873-1111-4111-8111-111111111111", label: "F" }]);
    const { result, rerender } = renderHook(
      ({ userId }: { userId: string }) => useAgentSessions("friday", userId),
      {
        initialProps: { userId: "user_1" },
        wrapper: ({ children }: { children: React.ReactNode }) => (
          <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
        ),
      },
    );
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    stubSessionsList([{ key: "agent:friday:user_2:0b477873-2222-4222-8222-222222222222", label: "G" }]);
    rerender({ userId: "user_2" });
    await waitFor(() => expect(result.current.sessions.map((s) => s.label)).toEqual(["G"]));
    expect(callRpc.mock.calls.filter(([m]) => m === "sessions.list")).toHaveLength(2);
  });

  it("returns empty and does not query when agentId is null", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions(null, "user-1");
    expect(result.current.sessions).toEqual([]);
    expect(callRpc).not.toHaveBeenCalledWith("sessions.list", expect.anything());
  });

  it("matches case-insensitively — lowercased sessions.list keys vs a mixed-case Clerk id", async () => {
    // OpenClaw lowercases the whole session key on store/lookup, so sessions.list
    // returns lowercased keys while the Clerk user/agent id passed in is
    // mixed-case. Regression guard: a case-sensitive compare dropped every row,
    // rendering an empty sidebar and never hydrating the pill.
    stubSessionsList([
      { key: "agent:agent-x:user_2abcdef:11111111-1111-4111-8111-111111111111", updatedAt: 200 },
      { key: "agent:agent-x:user_2abcdef", updatedAt: 100 }, // legacy 3-part, lowercased
    ]);
    const { result } = renderAgentSessions("Agent-X", "user_2AbcDEF");

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    expect(result.current.sessions.map((s) => s.sessionId)).toEqual([
      "11111111-1111-4111-8111-111111111111",
      null, // the legacy row still surfaces
    ]);
  });

  it("carries the untruncated title as rawLabel alongside the truncated display label", async () => {
    // A user-set label is the case that still reaches the 48-char display cap:
    // it is used verbatim, where a derived title is shortened to a name first.
    const longTitle = "b".repeat(60);
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        label: longTitle,
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].label).toBe(`${longTitle.slice(0, 48)}…`);
    expect(result.current.sessions[0].rawLabel).toBe(longTitle);
  });

  it("carries lastMessagePreview onto the row as `preview`", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        derivedTitle: "Taxes",
        lastMessagePreview: "  I checked the numbers  ",
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].preview).toBe("I checked the numbers");
  });

  it("does not fall back to `title` or `lastMessage` — fields the RPC never sent", async () => {
    // Both were in the label chain and both branches were unreachable: the row
    // carries `lastMessagePreview` (top-level) and no `title` at all. A row
    // with only those keys must land on the id fallback, not on their values.
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        title: "from a field that does not exist",
        lastMessage: { text: "nor does this one" },
        updatedAt: 200,
      } as never,
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].label).toBe("Session 11111111");
    expect(result.current.sessions[0].preview).toBeUndefined();
  });

  it("names a session from its first message without echoing it verbatim", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        derivedTitle: "hey can you check the Q3 numbers",
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].label).toBe("Check the Q3 numbers");
    // The rename editor seeds from the same cleaned title, not the raw message.
    expect(result.current.sessions[0].rawLabel).toBe("Check the Q3 numbers");
  });

  it("never rewrites a name the owner set themselves", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        label: "can you believe it",
        derivedTitle: "hey can you check the Q3 numbers",
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].label).toBe("can you believe it");
  });

  it("titles without writing anything back to the container", async () => {
    // The cleanup is deterministic and recomputed on every read, so there is
    // nothing to persist. Writing it as a sessions.patch {label} would occupy
    // the user-set-name slot permanently with a regex's output.
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        derivedTitle: "hey can you check the Q3 numbers",
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(callRpc.mock.calls.filter(([m]) => m === "sessions.patch")).toHaveLength(0);
  });

  it("carries run outcome onto the session", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        status: "failed",
        lastRunError: "boom",
        updatedAt: 200,
      } as never,
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].status).toBe("failed");
    expect(result.current.sessions[0].lastRunError).toBe("boom");
  });

  it("ignores a run status of the wrong shape rather than trusting the wire", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        status: 7,
        lastRunError: { nested: true },
        abortedLastRun: "yes",
        updatedAt: 200,
      } as never,
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].status).toBeUndefined();
    expect(result.current.sessions[0].lastRunError).toBeUndefined();
    expect(result.current.sessions[0].abortedLastRun).toBeUndefined();
  });

  it("prefers a user-set label over derived titles", async () => {
    stubSessionsList([
      {
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        label: "Taxes 2026",
        derivedTitle: "help me with my taxes",
        updatedAt: 200,
      },
    ]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].label).toBe("Taxes 2026");
  });

  it("a failed rename surfaces a toast instead of rejecting silently", async () => {
    callRpc.mockImplementation(async (method: string) => {
      if (method === "sessions.patch") throw new Error("gateway dropped");
      return { sessions: [] };
    });
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    // Must resolve (not reject) — the caller fire-and-forgets it.
    await result.current.renameSession("agent:agent-x:user-1:11111111-1111-4111-8111-111111111111", "X");
    expect(notifyApiError).toHaveBeenCalledWith(
      expect.any(Error),
      "Failed to rename session",
      expect.anything(),
    );
  });

  it("a failed revalidation after a successful patch does NOT toast a false failure", async () => {
    let listCalls = 0;
    callRpc.mockImplementation(async (method: string) => {
      if (method === "sessions.list") {
        listCalls += 1;
        // First call is the initial fetch (succeeds); the second is the
        // mutate()-triggered revalidation from renameSession, which fails.
        if (listCalls > 1) throw new Error("revalidate hiccup");
        return { sessions: [] };
      }
      return {}; // sessions.patch succeeds
    });
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await result.current.renameSession("agent:agent-x:user-1:11111111-1111-4111-8111-111111111111", "X");
    // Proves the revalidation attempt actually ran (and failed) rather than
    // this assertion passing vacuously because mutate() was never invoked.
    expect(listCalls).toBeGreaterThan(1);
    expect(notifyApiError).not.toHaveBeenCalled();
  });

  it("renameSession patches the label and revalidates", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await result.current.renameSession(
      "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
      "Taxes 2026",
    );
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
      label: "Taxes 2026",
    });
  });

  it("deleteSession deletes the entry (archiving its transcript) and revalidates", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const listCallsBefore = callRpc.mock.calls.filter(([m]) => m === "sessions.list").length;
    await result.current.deleteSession(
      "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
    );
    expect(callRpc).toHaveBeenCalledWith("sessions.delete", {
      key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
      deleteTranscript: true,
    });
    // A successful delete calls SWR's mutate(), which re-invokes the paging
    // fetcher — observed here as another sessions.list round trip.
    await waitFor(() =>
      expect(
        callRpc.mock.calls.filter(([m]) => m === "sessions.list").length,
      ).toBeGreaterThan(listCallsBefore),
    );
  });

  it("deleteSession propagates RPC failure (the confirm dialog surfaces it)", async () => {
    callRpc.mockImplementation(async (method: string) => {
      if (method === "sessions.delete") throw new Error("gateway unavailable");
      return { sessions: [] };
    });
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await expect(
      result.current.deleteSession("agent:agent-x:user-1:11111111-1111-4111-8111-111111111111"),
    ).rejects.toThrow("gateway unavailable");
  });

  it("a failed revalidation after a successful delete does NOT read as a failed delete", async () => {
    let listCalls = 0;
    callRpc.mockImplementation(async (method: string) => {
      if (method === "sessions.list") {
        listCalls += 1;
        if (listCalls > 1) throw new Error("revalidate hiccup");
        return { sessions: [] };
      }
      return {}; // sessions.delete succeeds
    });
    const { result } = renderAgentSessions("agent-x", "user-1");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await expect(
      result.current.deleteSession("agent:agent-x:user-1:11111111-1111-4111-8111-111111111111"),
    ).resolves.toBeUndefined();
    expect(listCalls).toBeGreaterThan(1);
  });
});

describe("useAgentSessions — pin and archive", () => {
  beforeEach(() => {
    callRpc.mockReset().mockResolvedValue({});
    notifyApiError.mockClear();
  });

  it("puts pinned rows above newer unpinned ones, and keeps recency inside each group", async () => {
    stubSessionsList([
      { key: "agent:a:u:11111111-1111-4111-8111-111111111111", derivedTitle: "newest", updatedAt: 900 },
      { key: "agent:a:u:22222222-2222-4222-8222-222222222222", derivedTitle: "pinned old", updatedAt: 100, pinned: true },
      { key: "agent:a:u:33333333-3333-4333-8333-333333333333", derivedTitle: "pinned new", updatedAt: 500, pinned: true },
    ]);
    const { result } = renderAgentSessions("a", "u");
    await waitFor(() => expect(result.current.sessions).toHaveLength(3));
    expect(result.current.sessions.map((s) => s.label)).toEqual([
      "Pinned new",
      "Pinned old",
      "Newest",
    ]);
  });

  it("does not ask the container for archived rows on the active rail", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("a", "u");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const [, params] = callRpc.mock.calls.find(([m]) => m === "sessions.list") as [string, Record<string, unknown>];
    // Omitted, not false: the container already reads an absent flag as
    // "active only", and sending false would be a second way to say it.
    expect(params).not.toHaveProperty("archived");
  });

  it("asks for archived rows only in archived mode", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("a", "u", true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const [, params] = callRpc.mock.calls.find(([m]) => m === "sessions.list") as [string, Record<string, unknown>];
    expect(params.archived).toBe(true);
  });

  it("archives with sessions.patch and reports that it landed", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("a", "u");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await expect(result.current.setArchived("agent:a:u:k", true)).resolves.toBe(true);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", { key: "agent:a:u:k", archived: true });
  });

  it("reports a refused archive as false instead of throwing", async () => {
    // The container refuses to archive a session with a run still in flight.
    // The caller uses the return value to decide whether to move the owner off
    // the session — a refusal must leave them exactly where they are.
    callRpc.mockImplementation(async (method: string) => {
      if (method === "sessions.list") return { sessions: [], hasMore: false };
      throw new Error("Session is still active; retry the archive.");
    });
    const { result } = renderAgentSessions("a", "u");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await expect(result.current.setArchived("agent:a:u:k", true)).resolves.toBe(false);
    expect(notifyApiError).toHaveBeenCalled();
  });

  it("restores with archived:false", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("a", "u", true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await result.current.setArchived("agent:a:u:k", false);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", { key: "agent:a:u:k", archived: false });
  });

  it("pins with sessions.patch", async () => {
    stubSessionsList([]);
    const { result } = renderAgentSessions("a", "u");
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await result.current.setPinned("agent:a:u:k", true);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", { key: "agent:a:u:k", pinned: true });
  });
});
