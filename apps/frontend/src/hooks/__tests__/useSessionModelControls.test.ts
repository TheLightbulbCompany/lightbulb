import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ModelEntry } from "@/lib/models";

const callRpc = vi.fn<(method: string, params?: Record<string, unknown>) => Promise<unknown>>(
  async () => ({}),
);
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpcMutation: () => callRpc,
}));
vi.mock("@clerk/nextjs", () => ({
  useUser: (): { user: { id: string } | null } => ({ user: { id: "user_1" } }),
}));

import {
  useSessionModelControls,
  LiveSessionModelSwitchError,
} from "@/hooks/useSessionModelControls";

const model: ModelEntry = {
  id: "arn:aws:bedrock:...:profile/abc.def",
  provider: "amazon-bedrock",
};

beforeEach(() => callRpc.mockClear());

describe("useSessionModelControls", () => {
  it("patches model with the exact provider/id ref against the 4-part key", async () => {
    const sid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", sid, false),
    );
    await result.current.setModel(model);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: `agent:agent_a:user_1:${sid}`,
      model: `amazon-bedrock/${model.id}`,
    });
  });

  it("targets the legacy 3-part key when activeSessionId is null", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, false),
    );
    await result.current.setThinkingLevel("high");
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent_a:user_1",
      thinkingLevel: "high",
    });
  });

  it("throws LiveSessionModelSwitchError and does NOT call RPC mid-run", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, true),
    );
    await expect(result.current.setModel(model)).rejects.toBeInstanceOf(
      LiveSessionModelSwitchError,
    );
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("patches fastMode as a boolean", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, false),
    );
    await result.current.setFastMode(true);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent_a:user_1",
      fastMode: true,
    });
  });
});
