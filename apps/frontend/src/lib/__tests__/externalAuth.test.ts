import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BROWSER_OPEN_FAILED_MSG,
  externalLinkClick,
  isTauri,
  openAuthUrl,
  pollUntil,
  returnsToApp,
} from "@/lib/externalAuth";

/* eslint-disable @typescript-eslint/no-explicit-any */

function installTauri(invoke = vi.fn().mockResolvedValue(undefined)) {
  (window as any).__TAURI__ = { core: { invoke } };
  return invoke;
}

afterEach(() => {
  delete (window as any).__TAURI__;
});

describe("externalAuth", () => {
  it("detects the Tauri runtime by __TAURI__.core.invoke", () => {
    expect(isTauri()).toBe(false);
    installTauri();
    expect(isTauri()).toBe(true);
  });

  it("openAuthUrl opens via the shell plugin under Tauri and reports it", async () => {
    const invoke = installTauri();
    await expect(openAuthUrl("https://provider.test/oauth")).resolves.toBe(true);
    expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
      path: "https://provider.test/oauth",
    });
  });

  it("openAuthUrl rejects with user-facing copy when the shell open fails", async () => {
    installTauri(vi.fn().mockRejectedValue("forbidden"));
    await expect(openAuthUrl("https://provider.test/oauth")).rejects.toThrow(
      BROWSER_OPEN_FAILED_MSG,
    );
  });

  it("externalLinkClick hijacks the anchor only under Tauri", () => {
    const e = { preventDefault: vi.fn() };
    externalLinkClick(e, "https://provider.test/device");
    expect(e.preventDefault).not.toHaveBeenCalled();

    const invoke = installTauri();
    externalLinkClick(e, "https://provider.test/device");
    expect(e.preventDefault).toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
      path: "https://provider.test/device",
    });
  });

  it("pollUntil resolves true once the check passes, treating errors as not-yet", async () => {
    let calls = 0;
    const ok = await pollUntil(
      async () => {
        calls += 1;
        if (calls === 1) throw new Error("transient");
        return calls >= 3;
      },
      { intervalMs: 1, timeoutMs: 5000 },
    );
    expect(ok).toBe(true);
    expect(calls).toBe(3);
  });

  it("pollUntil resolves false at the deadline", async () => {
    const ok = await pollUntil(async () => false, {
      intervalMs: 1,
      timeoutMs: 10,
    });
    expect(ok).toBe(false);
  });

  it("pollUntil stops checking once the signal aborts", async () => {
    const ac = new AbortController();
    let calls = 0;
    const promise = pollUntil(
      async () => {
        calls += 1;
        return false;
      },
      { intervalMs: 1, timeoutMs: 5000, signal: ac.signal },
    );
    // Let one check run, then abort — no further checks may fire.
    await new Promise((r) => setTimeout(r, 5));
    ac.abort();
    const settled = calls;
    await expect(promise).resolves.toBe(false);
    expect(calls).toBeLessThanOrEqual(settled + 1);
  });
});

describe("returnsToApp", () => {
  afterEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("has no app destination on plain web", () => {
    expect(returnsToApp()).toBeNull();
  });

  it("keeps the web landing for Tauri", () => {
    installTauri();
    expect(returnsToApp()).toBe("app");
  });

  it("latches ?from=app for the tab even after the URL changes", () => {
    window.history.replaceState(null, "", "/chat?panel=integrations&from=app");
    expect(returnsToApp()).toBe("ios");
    window.history.replaceState(null, "", "/chat?panel=connections");
    expect(returnsToApp()).toBe("ios");
  });
});
