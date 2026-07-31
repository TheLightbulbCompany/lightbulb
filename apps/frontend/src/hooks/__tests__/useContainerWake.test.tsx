// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const postSpy = vi.fn(async () => ({}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: postSpy }) }));

const presentSpy = vi.fn(() => true);
vi.mock("@/lib/presence", () => ({ isPresent: () => presentSpy() }));

describe("useContainerWake", () => {
  beforeEach(() => {
    vi.resetModules();
    postSpy.mockClear();
    presentSpy.mockReturnValue(true);
  });

  it("dedupes within the cooldown across hook instances (module-level state)", async () => {
    const { useContainerWake } = await import("@/hooks/useContainerWake");
    const a = renderHook(() => useContainerWake());
    const b = renderHook(() => useContainerWake());
    await act(async () => {
      await a.result.current.requestWake("interaction");
      // Second instance (a nested GatewayProvider) inside the cooldown
      // must be a no-op — one page, one wake budget.
      await b.result.current.requestWake("mount");
    });
    expect(postSpy).toHaveBeenCalledTimes(1);
    expect(postSpy).toHaveBeenCalledWith("/container/wake", { reason: "interaction" });
  });

  it("passive reasons are presence-gated; action reasons are not", async () => {
    const { useContainerWake } = await import("@/hooks/useContainerWake");
    presentSpy.mockReturnValue(false);
    const { result } = renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("mount");
      await result.current.requestWake("interaction");
    });
    expect(postSpy).not.toHaveBeenCalled();
    await act(async () => {
      await result.current.requestWake("message_send");
    });
    expect(postSpy).toHaveBeenCalledTimes(1);
  });

  it("soft-fails (never throws) even when the wake POST rejects", async () => {
    // No manual /container/status revalidation here anymore (spec PR-9,
    // CLIENT-HOOK deleted the wake/unpark/container_waking manual-mutate
    // trio) — the shared useOnboardingStatus bounded loop re-polls on its
    // own within its declared ceiling regardless of this call's outcome.
    postSpy.mockRejectedValueOnce(Object.assign(new Error("gated"), { status: 402 }));
    const { useContainerWake } = await import("@/hooks/useContainerWake");
    const { result } = renderHook(() => useContainerWake());
    await expect(
      act(async () => {
        await result.current.requestWake("interaction");
      }),
    ).resolves.not.toThrow();
  });
});
