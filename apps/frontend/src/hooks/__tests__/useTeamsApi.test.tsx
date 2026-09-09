import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import useSWR, { SWRConfig, useSWRConfig } from "swr";
import type { ReactNode } from "react";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { _resetTeamsEventsForTests } from "@/lib/teamsSubscription";

const auth = { isLoaded: true, userId: "user-a" as string | null, orgId: null as string | null };
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth }));
const gateway = { isConnected: true };
let eventHandler: (event: string, data: unknown) => void;
const send = vi.fn();
const onEvent = (handler: typeof eventHandler) => { eventHandler = handler; return () => {}; };
vi.mock("@/hooks/useGateway", () => ({ useGateway: () => ({ ...gateway, send, onEvent }) }));

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, post: mockPost, patch: mockPatch, del: mockDel }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

function deferred<T>() {
  let resolve!: (data: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  Object.assign(auth, { isLoaded: true, userId: "user-a", orgId: null });
  gateway.isConnected = true;
  _resetTeamsEventsForTests();
});

afterEach(() => vi.useRealTimers());

describe("Teams identity cache", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockDel.mockReset();
  });

  it.each(["user", "org"])("fetches immediately on %s turnover without retaining data over repeated renders or errors", async (dimension) => {
    const next = deferred<string>();
    mockGet.mockResolvedValueOnce("A").mockReturnValue(next.promise);
    const { result, rerender } = renderHook(() => useTeamsApi().read<string>("/agents", {
      keepPreviousData: true, shouldRetryOnError: false,
    }), { wrapper });
    await waitFor(() => expect(result.current.data).toBe("A"));
    if (dimension === "user") auth.userId = "user-b";
    else auth.orgId = "org-b";
    rerender();
    expect(result.current.data).toBeUndefined();
    rerender();
    expect(result.current.data).toBeUndefined();
    expect(mockGet).toHaveBeenCalledTimes(2);
    const denied = new Error("B denied");
    await act(async () => next.reject(denied));
    rerender();
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBe(denied);
  });

  it("retains previous query data only within the same identity, including after turnover", async () => {
    mockGet.mockResolvedValueOnce("A");
    const pending = deferred<string>();
    const { result, rerender } = renderHook(({ path }) => useTeamsApi().read<string>(path, {
      keepPreviousData: true,
    }), { wrapper, initialProps: { path: "/artifacts?q=one" } });
    await waitFor(() => expect(result.current.data).toBe("A"));
    mockGet.mockReturnValue(pending.promise);
    rerender({ path: "/artifacts?q=two" });
    expect(result.current.data).toBe("A");
    auth.orgId = "org-b";
    rerender({ path: "/artifacts?q=two" });
    expect(result.current.data).toBeUndefined();
    await act(async () => pending.resolve("B"));
    expect(result.current.data).toBe("B");
    mockGet.mockReturnValue(new Promise(() => {}));
    rerender({ path: "/artifacts?q=three" });
    expect(result.current.data).toBe("B");
  });

  it.each(["loading", "signed-out", "null-path"])("hides previous data at the %s boundary and starts no read", async (boundary) => {
    mockGet.mockResolvedValue("A");
    const { result, rerender } = renderHook(({ path }) => useTeamsApi().read<string>(path, {
      keepPreviousData: true,
    }), { wrapper, initialProps: { path: "/agents" as string | null } });
    await waitFor(() => expect(result.current.data).toBe("A"));
    if (boundary === "loading") auth.isLoaded = false;
    if (boundary === "signed-out") auth.userId = null;
    rerender({ path: boundary === "null-path" ? null : "/agents" });
    rerender({ path: boundary === "null-path" ? null : "/agents" });
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBeUndefined();
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("does not expose previous errors and isolates a late A response", async () => {
    const a = deferred<string>();
    const b = deferred<string>();
    mockGet.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const { result, rerender } = renderHook(() => useTeamsApi().read<string>("/agents", {
      shouldRetryOnError: false,
    }), { wrapper });
    auth.userId = "user-b";
    rerender();
    expect(mockGet).toHaveBeenCalledTimes(2);
    await act(async () => a.resolve("A late"));
    expect(result.current.data).toBeUndefined();
    await act(async () => b.reject(new Error("B error")));
    auth.isLoaded = false;
    rerender();
    expect(result.current.error).toBeUndefined();
    auth.isLoaded = true;
    auth.userId = "user-a";
    rerender();
    expect(result.current.data).toBe("A late");
    expect(result.current.error).toBeUndefined();
  });

  it("dedupes same-identity reads and never adopts a legacy raw Teams key", async () => {
    mockGet.mockResolvedValue("scoped");
    const { result, rerender } = renderHook(({ path }) => ({
      global: useSWRConfig().mutate,
      a: useTeamsApi().read(path), b: useTeamsApi().read(path),
    }), { wrapper, initialProps: { path: null as string | null } });
    await act(async () => { await result.current.global("/teams/agents", "legacy", false); });
    rerender({ path: "/agents" });
    expect(result.current.a.data).toBeUndefined();
    await waitFor(() => expect(result.current.a.data).toBe("scoped"));
    expect(result.current.b.data).toBe("scoped");
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("scopes exact, predicate, optimistic and captured bound mutations, preserving updater/options semantics", async () => {
    mockGet.mockImplementation(async () => auth.userId);
    const { result, rerender } = renderHook(() => {
      const teams = useTeamsApi();
      return { teams, read: teams.read<string>("/agents") };
    }, { wrapper });
    await waitFor(() => expect(result.current.read.data).toBe("user-a"));
    const old = result.current;
    auth.userId = "user-b";
    rerender();
    await waitFor(() => expect(result.current.read.data).toBe("user-b"));
    await act(async () => {
      await old.teams.mutate<string>("/teams/agents", (value) => `${value}:exact`, false);
      await old.teams.mutate<string>((path) => path === "/teams/agents", (value) => `${value}:predicate`, { revalidate: false });
      await old.read.mutate((value) => `${value}:bound`, false);
    });
    expect(result.current.read.data).toBe("user-b");
    auth.userId = "user-a";
    rerender();
    expect(result.current.read.data).toBe("user-a:exact:predicate:bound");
    const pending = deferred<string>();
    let mutation!: Promise<unknown>;
    act(() => {
      mutation = result.current.teams.mutate("/teams/agents", pending.promise, {
        optimisticData: "optimistic", rollbackOnError: true, revalidate: false,
      });
    });
    expect(result.current.read.data).toBe("optimistic");
    await act(async () => { pending.reject(new Error("failed")); await mutation.catch(() => {}); });
    expect(result.current.read.data).toBe("user-a:exact:predicate:bound");
    await act(async () => { await result.current.teams.mutate("/teams/agents", undefined, false); });
    rerender();
    expect(result.current.read.data).toBeUndefined();
  });

  it("a removal settling after turnover optimistically edits only its starting identity", async () => {
    mockGet.mockResolvedValue({ agents: [{ id: "a" }, { id: "b" }] });
    const pending = deferred<void>();
    mockDel.mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(() => ({
      data: useTeamsApi().read<{ agents: { id: string }[] }>("/agents").data,
      mutations: useAgentMutations(),
    }), { wrapper });
    await waitFor(() => expect(result.current.data?.agents).toHaveLength(2));
    const removal = result.current.mutations.remove("a");
    auth.orgId = "org-b";
    rerender();
    await waitFor(() => expect(result.current.data?.agents).toHaveLength(2));
    await act(async () => { pending.resolve(); await removal; });
    expect(result.current.data?.agents).toHaveLength(2);
    auth.orgId = null;
    rerender();
    expect(result.current.data?.agents).toEqual([{ id: "b" }]);
  });

  it("mixed invalidation includes non-Teams keys and current Teams, excluding legacy and foreign Teams", async () => {
    mockGet.mockResolvedValue("initial");
    const { result, rerender } = renderHook(({ dormantPath }) => {
      const teams = useTeamsApi();
      return { teams, global: useSWRConfig().mutate, read: teams.read("/agents"),
        dormant: teams.read(dormantPath),
        other: useSWR(["sessions", "user-a"], null, { fallbackData: "session" }),
        raw: useSWR("/teams/agents", null, { fallbackData: "legacy" }) };
    }, { wrapper, initialProps: { dormantPath: null as string | null } });
    await waitFor(() => expect(result.current.read.data).toBe("initial"));
    // An exact optimistic mutation can create a cache entry before any read
    // registers its key. SWR supplies undefined to predicates for that entry.
    await act(async () => { await result.current.teams.mutate("/teams/dormant", "A dormant", false); });
    auth.orgId = "org-b";
    rerender({ dormantPath: null });
    await waitFor(() => expect(result.current.read.data).toBe("initial"));
    await act(async () => { await result.current.global(result.current.teams.isCurrentScopeOrNonTeamsKey, "changed", false); });
    expect(result.current.read.data).toBe("changed");
    expect(result.current.other.data).toBe("changed");
    expect(result.current.raw.data).toBe("legacy");
    auth.orgId = null;
    rerender({ dormantPath: "/dormant" });
    expect(result.current.read.data).toBe("initial");
    expect(result.current.dormant.data).toBe("A dormant");
  });

  it("resets old-scope trailing work and throttles, reconciles turnover/reconnect, and keeps only the visible current board fresh", async () => {
    vi.useFakeTimers();
    mockGet.mockResolvedValue("board");
    function eventsWrapper({ children }: { children: ReactNode }) {
      return <SWRConfig value={{ provider: () => new Map() }}><TeamsEventsProvider>{children}</TeamsEventsProvider></SWRConfig>;
    }
    const { rerender } = renderHook(() => useTeamsApi().read("/issues"), { wrapper: eventsWrapper });
    await act(async () => {});
    await act(async () => { eventHandler("teams.activity.logged", {}); });
    act(() => { eventHandler("teams.activity.logged", {}); });
    const oldHandler = eventHandler;
    auth.orgId = "org-b";
    await act(async () => rerender());
    const afterTurnover = mockGet.mock.calls.length;
    await act(async () => { oldHandler("teams.activity.logged", {}); });
    await act(async () => { eventHandler("teams.activity.logged", {}); });
    expect(mockGet).toHaveBeenCalledTimes(afterTurnover + 1);
    await act(async () => vi.advanceTimersByTime(10_000));
    expect(mockGet).toHaveBeenCalledTimes(afterTurnover + 1);
    gateway.isConnected = false;
    rerender();
    gateway.isConnected = true;
    await act(async () => rerender());
    expect(mockGet).toHaveBeenCalledTimes(afterTurnover + 2);
    const visible = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => vi.advanceTimersByTime(20_000));
    expect(mockGet).toHaveBeenCalledTimes(afterTurnover + 2);
    visible.mockReturnValue("visible");
    await act(async () => vi.advanceTimersByTime(20_000));
    expect(mockGet).toHaveBeenCalledTimes(afterTurnover + 3);
    visible.mockRestore();
  });
});

// The opts the Teams layer must forward on every BFF call so the browser
// attaches the `.flickmeon.ai` Paperclip cookie and retries once on a rotated
// session.
const EXPECTED_OPTS = { credentials: "include", retryOn401: true };

describe("useTeamsApi cookie opt-ins", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
  });

  it("forwards credentials + retryOn401 on reads, prefixing /teams", async () => {
    mockGet.mockResolvedValue({ ok: true });

    // `read` is itself a hook (wraps useSWR), so call it at the top of a
    // rendered hook body — not inside a nested function.
    renderHook(() => useTeamsApi().read("/dashboard"), { wrapper });

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith("/teams/dashboard", EXPECTED_OPTS);
    });
  });

  it("forwards opts on post/patch/del", async () => {
    mockPost.mockResolvedValue({ ok: true });
    mockPatch.mockResolvedValue({ ok: true });
    mockDel.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useTeamsApi(), { wrapper });

    await result.current.post("/agents", { name: "a" });
    await result.current.patch("/agents/1", { name: "b" });
    await result.current.del("/agents/1");

    expect(mockPost).toHaveBeenCalledWith("/teams/agents", { name: "a" }, EXPECTED_OPTS);
    expect(mockPatch).toHaveBeenCalledWith("/teams/agents/1", { name: "b" }, EXPECTED_OPTS);
    expect(mockDel).toHaveBeenCalledWith("/teams/agents/1", EXPECTED_OPTS);
  });

  it("uses a unique SWR cache key per path", async () => {
    mockGet.mockImplementation((p: string) =>
      Promise.resolve(p === "/teams/agents" ? { list: "agents" } : { list: "issues" }),
    );

    function Both() {
      const api = useTeamsApi();
      const a = api.read<{ list: string }>("/agents");
      const b = api.read<{ list: string }>("/issues");
      return { a, b };
    }

    const { result } = renderHook(Both, { wrapper });

    await waitFor(() => expect(result.current.a.data).toEqual({ list: "agents" }));
    await waitFor(() => expect(result.current.b.data).toEqual({ list: "issues" }));
    expect(mockGet).toHaveBeenCalledWith("/teams/agents", EXPECTED_OPTS);
    expect(mockGet).toHaveBeenCalledWith("/teams/issues", EXPECTED_OPTS);
  });

  it("propagates fetcher errors to SWR.error", async () => {
    const boom = new Error("boom");
    mockGet.mockRejectedValueOnce(boom);
    const { result } = renderHook(
      () => useTeamsApi().read("/agents", { shouldRetryOnError: false }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).toBe(boom));
  });

  it("still re-throws the 202 provisioning sentinel from a read body", async () => {
    mockGet.mockResolvedValue({ detail: "team workspace provisioning" });

    const { result } = renderHook(
      () => useTeamsApi().read<unknown>("/dashboard", { shouldRetryOnError: false }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.error).toBeDefined();
    });
    expect((result.current.error as { status?: number }).status).toBe(202);
  });
});
