import { afterEach, describe, expect, it, vi } from "vitest";

import {
  externalLinkClick,
  isTauri,
  openAuthUrl,
  pollUntil,
} from "@/lib/externalAuth";

/* eslint-disable @typescript-eslint/no-explicit-any */

function installTauri() {
  const invoke = vi.fn();
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

  it("openAuthUrl opens via the shell plugin under Tauri and reports it", () => {
    const invoke = installTauri();
    expect(openAuthUrl("https://provider.test/oauth")).toBe(true);
    expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
      path: "https://provider.test/oauth",
    });
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
});
