import { describe, it, expect, beforeEach, afterAll, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";

// Node's built-in localStorage needs a flag vitest doesn't pass, so install the
// shared Map-backed polyfill (persists across vi.resetModules(), which is how we
// simulate a full page refresh: fresh module = fresh Map, same localStorage).
const restoreStorage = installStoragePolyfill();
afterAll(restoreStorage);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

describe("useActiveSession — shared per-agent session store", () => {
  beforeEach(() => {
    vi.resetModules();
    // The module Map is reset by resetModules, but localStorage persists across
    // tests — clear it so the persistence backing can't leak between cases.
    window.localStorage.clear();
  });

  it("defaults to the General session (null) for an agent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    expect(result.current.activeSessionId).toBeNull();
  });

  it("newSession() mints a lowercase UUIDv4 and sets it active", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    expect(minted).toMatch(UUID_RE);
    expect(result.current.activeSessionId).toBe(minted);
  });

  it("two hooks on the same agent see the same value (cross-subtree sharing)", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const a = renderHook(() => useActiveSession("agent-X"));
    const b = renderHook(() => useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession("11111111-1111-4111-8111-111111111111");
    });
    expect(b.result.current.activeSessionId).toBe(
      "11111111-1111-4111-8111-111111111111",
    );
  });

  it("keeps per-agent state independent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const x = renderHook(() => useActiveSession("agent-X"));
    const y = renderHook(() => useActiveSession("agent-Y"));
    act(() => x.result.current.newSession());
    expect(y.result.current.activeSessionId).toBeNull();
  });
});

// A full page refresh wipes the module-level Map, so the active session must be
// backed by localStorage: selecting a named session persists it and a fresh
// module load (simulated with vi.resetModules() while localStorage survives)
// restores it, instead of resetting every reload to General.
describe("useActiveSession — persistence across a full page refresh", () => {
  const NAMED = "22222222-2222-4222-8222-222222222222";

  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  /** Re-import the module after resetting it — a fresh module = fresh Map, the
   *  refresh scenario. localStorage is NOT reset, so it is the only bridge. */
  async function freshLoad() {
    vi.resetModules();
    return import("@/hooks/useActiveSession");
  }

  it("selecting a named session persists it and it survives a fresh module load", async () => {
    const first = await import("@/hooks/useActiveSession");
    const a = renderHook(() => first.useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession(NAMED);
    });
    expect(a.result.current.activeSessionId).toBe(NAMED);
    expect(window.localStorage.getItem("isol8:activeSession:agent-X")).toBe(NAMED);

    // Simulate a refresh: fresh module (empty Map), same localStorage.
    const second = await freshLoad();
    const b = renderHook(() => second.useActiveSession("agent-X"));
    expect(b.result.current.activeSessionId).toBe(NAMED);
  });

  it("newSession() persists the minted id across a fresh module load", async () => {
    const first = await import("@/hooks/useActiveSession");
    const a = renderHook(() => first.useActiveSession("agent-X"));
    let minted = "";
    act(() => {
      minted = a.result.current.newSession();
    });
    expect(minted).toMatch(UUID_RE);

    const second = await freshLoad();
    const b = renderHook(() => second.useActiveSession("agent-X"));
    expect(b.result.current.activeSessionId).toBe(minted);
  });

  it("selecting General (null) clears the stored session so a refresh keeps General, not a stale named session", async () => {
    const first = await import("@/hooks/useActiveSession");
    const a = renderHook(() => first.useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession(NAMED);
    });
    expect(window.localStorage.getItem("isol8:activeSession:agent-X")).toBe(NAMED);

    // Switch back to General — the stored key must be cleared.
    act(() => {
      a.result.current.selectSession(null);
    });
    expect(a.result.current.activeSessionId).toBeNull();
    expect(window.localStorage.getItem("isol8:activeSession:agent-X")).toBeNull();

    // A refresh now restores General (null), not the stale named session.
    const second = await freshLoad();
    const b = renderHook(() => second.useActiveSession("agent-X"));
    expect(b.result.current.activeSessionId).toBeNull();
  });

  it("getActiveSessionId (non-hook) also hydrates the persisted session after a fresh module load", async () => {
    const first = await import("@/hooks/useActiveSession");
    const a = renderHook(() => first.useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession(NAMED);
    });

    const second = await freshLoad();
    // Before any hook read for this agent, the non-hook accessor must still
    // resolve the persisted value (RunNotifications' desktop path relies on it).
    expect(second.getActiveSessionId("agent-X")).toBe(NAMED);
  });

  it("survives when localStorage access throws (private mode) — falls back to the in-memory Map", async () => {
    // Safari private mode throws on any localStorage access — simulate by
    // making the property getter itself throw.
    const desc = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError: localStorage disabled");
      },
    });
    try {
      const { useActiveSession } = await import("@/hooks/useActiveSession");
      const a = renderHook(() => useActiveSession("agent-X"));
      // Default read (accessing localStorage throws) must not crash → General.
      expect(a.result.current.activeSessionId).toBeNull();
      // Writing (accessing localStorage throws) must not crash; the Map still
      // reflects it within this session.
      act(() => {
        a.result.current.selectSession(NAMED);
      });
      expect(a.result.current.activeSessionId).toBe(NAMED);
    } finally {
      if (desc) Object.defineProperty(window, "localStorage", desc);
    }
  });
});
