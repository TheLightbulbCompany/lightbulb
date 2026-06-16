import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { DreamsPanel } from "../DreamsPanel";

const rpcMock = vi.fn();
const patchConfigMock = vi.fn().mockResolvedValue({ ok: true });
// Overridable per-test: the real `agents.list` payload shape is
// `{ defaultId?, agents? }`, NOT a bare array.
let agentsListData: { defaultId?: string; agents?: Array<{ id: string; identity?: { name?: string } }> } =
  { defaultId: "main", agents: [{ id: "main", identity: { name: "Friday" } }] };
vi.mock("@/hooks/useConfigPatch", () => ({
  useConfigPatch: () => ({
    config: {}, configReady: true, patchConfig: patchConfigMock, refreshConfig: vi.fn(),
  }),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null) => {
    if (method === "agents.list") {
      return { data: agentsListData, error: undefined, isLoading: false, mutate: vi.fn() };
    }
    if (method === "doctor.memory.status") {
      return {
        data: { agentId: "main", embedding: { ok: true }, dreaming: { enabled: true, promotedToday: 2, totalSignalCount: 5, phases: { light: { enabled: true }, rem: { enabled: true }, deep: { enabled: false } } } },
        error: undefined, isLoading: false, mutate: vi.fn(),
      };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useGatewayRpcMutation: () => rpcMock,
}));

describe("DreamsPanel", () => {
  beforeEach(() => {
    rpcMock.mockReset();
    agentsListData = { defaultId: "main", agents: [{ id: "main", identity: { name: "Friday" } }] };
  });

  it("renders the Scene tab with dreaming status by default", async () => {
    render(<DreamsPanel />);
    await waitFor(() => expect(screen.getByText(/Dreaming/i)).toBeInTheDocument());
    // promotedToday surfaced
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("shows the three sleep-phase indicators", async () => {
    render(<DreamsPanel />);
    await waitFor(() => expect(screen.getByText(/Light/i)).toBeInTheDocument());
    expect(screen.getByText(/REM/i)).toBeInTheDocument();
    expect(screen.getByText(/Deep/i)).toBeInTheDocument();
  });

  it("renders the agent selector with each agent's display name when multiple agents exist", async () => {
    // Real `agents.list` shape: object with `agents` array, ids under `id`,
    // labels resolved via identity.name. The old code read a bare array and
    // `a.agent_id`, so the selector never rendered for multi-agent owners.
    agentsListData = {
      defaultId: "main",
      agents: [
        { id: "main", identity: { name: "Friday" } },
        { id: "agent_atlas", identity: { name: "Atlas" } },
      ],
    };
    render(<DreamsPanel />);
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /Friday/ })).toBeInTheDocument(),
    );
    expect(screen.getByRole("option", { name: /Atlas/ })).toBeInTheDocument();
  });

  it("hides the agent selector when only one agent exists", async () => {
    render(<DreamsPanel />);
    await waitFor(() => expect(screen.getByText(/Dreaming/i)).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: /Friday/ })).not.toBeInTheDocument();
  });

  it("toggles a phase via config.patch", async () => {
    patchConfigMock.mockClear();
    render(<DreamsPanel />);
    const btn = await screen.findByLabelText(/Toggle Light phase/i);
    fireEvent.click(btn);
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith(
        expect.objectContaining({
          plugins: { entries: { "memory-core": { config: { dreaming: { phases: { light: { enabled: false } } } } } } },
        }),
      ),
    );
  });
});
