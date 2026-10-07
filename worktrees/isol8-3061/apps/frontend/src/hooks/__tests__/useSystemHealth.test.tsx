// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { useSystemHealth } from "../useSystemHealth";

const live = { liveConnected: true };

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => live,
}));
vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({
    container: { container_status: "running", lifecycle: "ready" },
  }),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: vi.fn().mockResolvedValue(null) }),
}));

describe("useSystemHealth live delivery", () => {
  beforeEach(() => {
    live.liveConnected = true;
  });

  it("reports healthy when the runtime and live delivery are ready", () => {
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("HEALTHY");
  });

  it("reports recovering while live delivery reconnects", () => {
    live.liveConnected = false;
    const { result } = renderHook(() => useSystemHealth());
    expect(result.current.state).toBe("RECOVERING");
    expect(result.current.canRecover).toBe(false);
  });
});
