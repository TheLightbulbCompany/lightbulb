import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const prov = vi.hoisted(() => ({ value: { phase: "normal", container: null as Record<string, unknown> | null } }));
vi.mock("@/hooks/useProvisioningState", () => ({ useProvisioningState: () => prov.value }));

import { useChatReady } from "../useChatReady";

describe("useChatReady", () => {
  it("setupStarting only while setup is genuinely under way: not when ready, failed, or stopped at a gate", () => {
    const at = (phase: string, container: Record<string, unknown> | null) => {
      prov.value = { phase, container };
      return renderHook(() => useChatReady()).result.current;
    };
    expect(at("normal", { runtime_phase: "provisioning", container_status: "provisioning" })).toEqual({ chatReady: false, setupStarting: true });
    expect(at("normal", null)).toEqual({ chatReady: false, setupStarting: true });
    expect(at("normal", { runtime_phase: "ready", container_status: "running" })).toEqual({ chatReady: true, setupStarting: false });
    expect(at("normal", { runtime_phase: "provisioning", container_status: "error", lifecycle: "error" }).setupStarting).toBe(false);
    expect(at("blocked", null).setupStarting).toBe(false);
  });
});
