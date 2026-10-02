/**
 * The model picker and the identity card come from what the runtime really has:
 * `models.list` and the roster row `agents.list` already returned. Neither the
 * stubbed `agent.identity.get` nor `config.get` is dialled — each was a ~55s
 * microVM start to render em-dashes. Heartbeats have no equivalent here, so the
 * card is gone rather than inert.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import userEvent from "@testing-library/user-event";
import React from "react";

import { AgentOverviewTab } from "../AgentOverviewTab";

const apiGet = vi.fn();
const apiPut = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet, put: apiPut }) }));
const render = (ui: React.ReactElement) =>
  rtlRender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>);

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
    apiGet.mockReset();
    apiPut.mockReset();
    apiGet.mockResolvedValue({ name: "Ada", notes: "be blunt", voice: "Marin" });
  });

  describe("Voice card", () => {
    it("loads voice and notes with a counter", async () => {
      render(<AgentOverviewTab agentId="a1" agent={agent} />);
      expect(await screen.findByText("Marin")).toBeInTheDocument();
      expect(screen.getByDisplayValue("be blunt")).toBeInTheDocument();
      expect(screen.getByText("8 / 1,000")).toBeInTheDocument();
      expect(screen.getByText(/How Ada talks in Live and chat\. Ada adds to this/)).toBeInTheDocument();
    });

    it("saves with PUT {notes}, and Cancel restores", async () => {
      const user = userEvent.setup();
      apiPut.mockResolvedValue({ name: "Ada", notes: "be blunt!", voice: "Marin" });
      render(<AgentOverviewTab agentId="a1" agent={agent} />);
      const box = await screen.findByDisplayValue("be blunt");
      await user.type(box, "!");
      await user.click(screen.getAllByRole("button", { name: "Save" })[0]);
      await waitFor(() => expect(apiPut).toHaveBeenCalledWith("/teams/agents/a1/voice-notes", { notes: "be blunt!" }));
      await user.type(box, "x");
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.getByDisplayValue("be blunt!")).toBeInTheDocument();
    });

    it("keeps the draft on a 422", async () => {
      const user = userEvent.setup();
      apiPut.mockRejectedValue(new Error("too long"));
      render(<AgentOverviewTab agentId="a1" agent={agent} />);
      await user.type(await screen.findByDisplayValue("be blunt"), "?");
      await user.click(screen.getAllByRole("button", { name: "Save" })[0]);
      expect(await screen.findByText("too long")).toBeInTheDocument();
      expect(screen.getByDisplayValue("be blunt?")).toBeInTheDocument();
    });

    it("resets when the agent changes", async () => {
      const user = userEvent.setup();
      const { rerender } = render(<AgentOverviewTab agentId="a1" agent={agent} />);
      await user.type(await screen.findByDisplayValue("be blunt"), "?");
      apiGet.mockResolvedValue({ name: "Bo", notes: "calm", voice: "Cedar" });
      rerender(
        <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
          <AgentOverviewTab agentId="a2" agent={{ ...agent, id: "a2", name: "Bo" }} />
        </SWRConfig>,
      );
      expect(await screen.findByDisplayValue("calm")).toBeInTheDocument();
    });

    it("says so when voice settings fail to load, rest of tab intact", async () => {
      apiGet.mockRejectedValue(new Error("nope"));
      render(<AgentOverviewTab agentId="a1" agent={agent} />);
      expect(await screen.findByText("Voice settings unavailable")).toBeInTheDocument();
      expect(screen.getByText("Backend engineer")).toBeInTheDocument();
    });
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
