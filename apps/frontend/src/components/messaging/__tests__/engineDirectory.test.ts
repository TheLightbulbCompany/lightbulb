import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useEngineDirectory } from "../engineDirectory";

const rec = (extra = {}) => ({ session_id: "a/claude:1", wakes: false, ...extra });
let ask: ReturnType<typeof vi.fn>;
const install = (directory: () => Promise<unknown>) => {
  ask = vi.fn((cmd: string, args?: { op?: string }) =>
    args?.op === "" ? Promise.resolve(null) : args?.op === "directory" ? directory() : Promise.reject("?"));
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke: ask } };
};
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const calls = () => ask.mock.calls.filter((c) => c[1]?.op === "directory").length;

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); delete (window as unknown as { __TAURI__?: unknown }).__TAURI__; });

describe("useEngineDirectory", () => {
  it("is empty and silent with no bridge", async () => {
    const { result } = renderHook(() => useEngineDirectory());
    await tick(6000);
    expect(result.current).toEqual({ records: [], down: false });
  });

  it("is empty and silent when the app has no engine_ask", async () => {
    install(() => Promise.resolve({ ok: true, sessions: [rec()], ended: [] }));
    ask.mockImplementation(() => Promise.reject("Command engine_ask not found"));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(6000);
    expect(result.current).toEqual({ records: [], down: false });
    expect(calls()).toBe(0);
    unmount();
  });

  it("reads records, keeps them through one reject, recovers, polls every 5 s and stops on unmount", async () => {
    let n = 0;
    install(() => (++n === 2 ? Promise.reject("socket") : Promise.resolve({ ok: true, sessions: [rec()], ended: [] })));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    expect(result.current).toEqual({ records: [rec()], down: false });
    await tick(5000);
    expect(n).toBe(2);
    expect(result.current).toEqual({ records: [rec()], down: false });
    await tick(5000);
    expect(result.current.down).toBe(false);
    expect(result.current.records).toHaveLength(1);
    unmount();
    const before = calls();
    await tick(20000);
    expect(calls()).toBe(before);
  });

  it("goes down on the second failure in a row, of either kind, and recovers", async () => {
    const answers: (() => Promise<unknown>)[] = [
      () => Promise.resolve({ ok: true, sessions: [rec()], ended: [] }),
      () => Promise.reject("socket"),
      () => Promise.resolve({ ok: false }),
      () => Promise.resolve({ ok: true, sessions: [rec()], ended: [] }),
      () => Promise.resolve({ ok: false }),
      () => Promise.resolve({ ok: true, sessions: [rec()], ended: [] }),
    ];
    install(() => answers.shift()!());
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    await tick(5000);
    expect(result.current).toEqual({ records: [rec()], down: false });
    await tick(5000);
    expect(result.current).toEqual({ records: [], down: true });
    await tick(5000);
    expect(result.current).toEqual({ records: [rec()], down: false });
    await tick(5000);
    expect(result.current).toEqual({ records: [rec()], down: false });
    unmount();
  });

  it("treats records without wakes (a 0.7.x engine) as absent, not down", async () => {
    install(() => Promise.resolve({ ok: true, sessions: [{ session_id: "x" }], ended: [] }));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    expect(result.current).toEqual({ records: [], down: false });
    unmount();
  });

  it("an {ok:false} first answer is silent, the second is down", async () => {
    install(() => Promise.resolve({ ok: false }));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    expect(result.current).toEqual({ records: [], down: false });
    await tick(5000);
    expect(result.current).toEqual({ records: [], down: true });
    unmount();
  });

  it("skips a tick while a call is pending and resumes after it settles", async () => {
    let release!: (v: unknown) => void;
    let n = 0;
    install(() => (++n === 1 ? new Promise((r) => { release = r; }) : Promise.resolve({ ok: true, sessions: [rec()], ended: [] })));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    await tick(10000);
    expect(calls()).toBe(1);
    release({ ok: true, sessions: [rec()], ended: [] });
    await tick(5000);
    expect(calls()).toBe(2);
    expect(result.current.records).toHaveLength(1);
    unmount();
  });

  it("an equal poll keeps the same records array", async () => {
    install(() => Promise.resolve({ ok: true, sessions: [rec()], ended: [] }));
    const { result, unmount } = renderHook(() => useEngineDirectory());
    await tick(0);
    const first = result.current.records;
    await tick(5000);
    expect(result.current.records).toBe(first);
    unmount();
  });
});
