/**
 * The model picker and the identity card come from what the runtime really has:
 * `models.list` and the roster row `agents.list` already returned. Neither the
 * stubbed `agent.identity.get` nor `config.get` is dialled — each was a ~55s
 * microVM start to render em-dashes. Heartbeats have no equivalent here, so the
 * card is gone rather than inert.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

import { AgentOverviewTab } from "../AgentOverviewTab";

const ARN_SONNET = "arn:aws:bedrock:us-east-1:1:application-inference-profile/son";
const ARN_OPUS = "arn:aws:bedrock:us-east-1:1:application-inference-profile/opu";

let statusLoaded = false;
const rpcCalls: { method: string | null }[] = [];
const mockCallRpc = vi.fn();

vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => statusLoaded }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcCalls.push({ method });
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => mockCallRpc,
}));
vi.mock("@/hooks/useConfigPatch", () => ({
  useConfigPatch: () => ({ config: undefined, configReady: false, patchConfig: vi.fn(), refreshConfig: vi.fn() }),
  useModelCatalog: () => ({
    source: {
      models: [
        { id: ARN_SONNET, name: "Claude Sonnet", provider: "bedrock", tier: "standard" },
        { id: ARN_OPUS, name: "Claude Opus", provider: "bedrock", tier: "premium" },
      ],
      defaultId: ARN_SONNET,
    },
    defaultId: ARN_SONNET,
    ready: true,
  }),
}));

const agent = {
  id: "a1",
  name: "Ada",
  role: "engineer",
  title: "Backend engineer",
  model: ARN_OPUS,
};

describe("AgentOverviewTab", () => {
  beforeEach(() => {
    rpcCalls.length = 0;
    mockCallRpc.mockReset();
    mockCallRpc.mockResolvedValue({ ok: true });
    statusLoaded = true;
  });

  it("dials neither the stubbed identity RPC nor config.get", () => {
    render(<AgentOverviewTab agentId="a1" agent={agent} />);
    expect(rpcCalls.every((call) => call.method === null)).toBe(true);
  });

  it("reads identity off the roster row instead of the stub", () => {
    render(<AgentOverviewTab agentId="a1" agent={agent} />);
    expect(screen.getByText("Ada")).toBeInTheDocument();
    // (the role form's <option> carries the same label, so scope to the identity row)
    expect(screen.getAllByText("Engineer").some((el) => el.tagName !== "OPTION")).toBe(true);
    expect(screen.getByText("Backend engineer")).toBeInTheDocument();
    // Hermes has no emoji/theme — those rows would always have read "—".
    expect(screen.queryByText("Emoji")).toBeNull();
    expect(screen.queryByText("Theme")).toBeNull();
  });

  it("shows the agent's real model by name and sends the picked id back", async () => {
    const user = userEvent.setup();
    render(<AgentOverviewTab agentId="a1" agent={agent} />);
    await user.click(screen.getByText("Claude Opus"));
    await user.click(await screen.findByText("Claude Sonnet"));
    expect(mockCallRpc).toHaveBeenCalledWith("agents.update", { agentId: "a1", model: ARN_SONNET });
  });

  it("seeds the role form from the roster row, so Save cannot silently demote the agent", () => {
    render(<AgentOverviewTab agentId="a1" agent={agent} />);
    expect((screen.getByLabelText("Role") as HTMLSelectElement).value).toBe("engineer");
  });

  it("holds every RPC until a status body has landed", () => {
    statusLoaded = false;
    render(<AgentOverviewTab agentId="a1" agent={agent} />);
    expect(rpcCalls.every((call) => call.method === null)).toBe(true);
  });

  // Hermes's scheduler is `cron` jobs and `cron.allow_agent_scheduling` is
  // pinned false, so there is no heartbeat to show or to write.
  it("shows no Heartbeat card", () => {
    render(<AgentOverviewTab agentId="a1" />);
    expect(screen.queryByText("Heartbeat")).toBeNull();
  });
});
