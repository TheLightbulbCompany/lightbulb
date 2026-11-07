import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { SWRConfig } from "swr";
import { AgentInspectorPanel } from "../AgentInspectorPanel";

// The inspector mounts the SAME per-agent control tabs AgentsPanel does. Stub
// them so we can assert which one is mounted without dragging their gateway /
// billing deps into the sandbox.
vi.mock("../AgentOverviewTab", () => ({
  AgentOverviewTab: ({
    agentId,
    onAgentUpdated,
  }: {
    agentId: string;
    onAgentUpdated: () => void;
  }) => (
    <div data-testid="overview-tab">
      overview:{agentId}
      <button type="button" onClick={onAgentUpdated}>refresh agent</button>
    </div>
  ),
}));
vi.mock("../AgentToolsTab", () => ({
  AgentToolsTab: ({ agentId }: { agentId: string }) => (
    <div data-testid="tools-tab">tools:{agentId}</div>
  ),
}));
vi.mock("../AgentInstructionsTab", () => ({
  AgentInstructionsTab: ({ agentId }: { agentId: string }) => (
    <div data-testid="instructions-tab">instructions:{agentId}</div>
  ),
}));

vi.mock("../SessionsTimeline", () => ({ SessionsTimeline: () => <div>sessions</div> }));
vi.mock("../AgentMemoryTab", () => ({
  AgentMemoryTab: () => <div data-testid="memory-tab">memory</div>,
}));

// agentId drives which agent the inspector inspects; initialTab deep-links the
// initial tab (set by the Get Started "Connect a channel" CTA). Both are now
// props fed by the shell — a small helper renders with the current fixtures.
let agentParam: string | null = "agent_42";
let inspectorTabParam: string | null = null;
// Some unmocked children use SWR directly. Isolate it per render so no
// revalidation or retry fires after the test environment is torn down
// (that surfaced as an intermittent "document is not defined").
const renderInspector = () =>
  render(
    <SWRConfig
      value={{
        provider: () => new Map(),
        shouldRetryOnError: false,
        revalidateOnFocus: false,
        revalidateOnReconnect: false,
        refreshInterval: 0,
        isVisible: () => true,
        isOnline: () => true,
      }}
    >
      <AgentInspectorPanel agentId={agentParam} initialTab={inspectorTabParam} />
    </SWRConfig>,
  );

const refreshSpy = vi.fn();
// agents.list feeds the header (name/emoji), mirroring AgentsPanel.
const agentsListData: { defaultId?: string; agents?: Array<{ id: string; name?: string; identity?: { emoji?: string } }> } =
  { defaultId: "main", agents: [{ id: "agent_42", name: "Scout", identity: { emoji: "🔭" } }] };
// The roster now comes from the shared useAgents hook (which reaches for the
// gateway context), so that is the seam to stub — not the RPC beneath it.
vi.mock("@/hooks/useAgents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useAgents")>();
  return {
    ...actual,
    useAgents: () => ({
      agents: agentsListData.agents ?? [],
      defaultId: agentsListData.defaultId,
      error: undefined,
      isLoading: false,
      isSettled: true,
      refresh: refreshSpy,
      createAgent: vi.fn(),
      deleteAgent: vi.fn(),
      updateAgent: vi.fn(),
    }),
  };
});

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    // NO "agents.list" branch on purpose: the roster must come from the
    // useAgents mock above, so reverting the component to a raw agents.list
    // call FAILS here instead of passing on this fixture.
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => vi.fn(),
}));


// The Overview tab folds in the Teams org controls (reports-to + lifecycle).
// Stub AgentOrgControls so we can assert it mounts with the matched roster
// agent without dragging its mutation deps into the sandbox.
vi.mock("@/components/teams/agents/AgentOrgControls", () => ({
  AgentOrgControls: ({ agent }: { agent: { id: string } }) => (
    <div data-testid="org-controls">org:{agent.id}</div>
  ),
}));

// The org section reads the Teams roster via useTeamsApi().read("/agents").
// `rosterData`/`rosterError` drive the graceful-degrade branches.
let rosterData: { agents?: Array<{ id: string; name: string }> } | undefined = {
  agents: [{ id: "agent_42", name: "Scout" }],
};
let rosterError: Error | undefined;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      path === "/agents"
        ? { data: rosterData, error: rosterError, isLoading: false, mutate: vi.fn() }
        : { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() },
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

describe("AgentInspectorPanel", () => {
  beforeEach(() => {
    agentParam = "agent_42";
    inspectorTabParam = null;
    rosterData = { agents: [{ id: "agent_42", name: "Scout" }] };
    rosterError = undefined;
  });

  it("names a roster agent agents.list does not know yet, and refetches agents.list", () => {
    agentParam = "agent_new";
    rosterData = { agents: [{ id: "agent_new", name: "Nova" }] };
    refreshSpy.mockClear();
    renderInspector();
    expect(screen.getByRole("heading", { name: "Nova" })).toBeInTheDocument();
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  it("renders the tab labels", () => {
    renderInspector();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Instructions" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tools" })).toBeInTheDocument();
    // The Paperclip Dashboard, Runs and Schedule tabs are gone (Phase 1 task 7).
    expect(screen.queryByRole("tab", { name: "Schedule" })).toBeNull();
  });

  it("orders the tabs Overview·Instructions·Memory·Sessions·Tools", () => {
    renderInspector();
    const labels = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(labels).toEqual(["Overview", "Instructions", "Memory", "Sessions", "Tools"]);
  });

  it("mounts the Instructions tab with the ?agent id when selected", () => {
    renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(screen.getByTestId("instructions-tab")).toHaveTextContent(
      "instructions:agent_42",
    );
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();
  });

  it("mounts the Overview tab by default with the ?agent id", () => {
    renderInspector();
    expect(screen.getByTestId("overview-tab")).toHaveTextContent("overview:agent_42");
    expect(screen.queryByTestId("tools-tab")).not.toBeInTheDocument();
  });

  it("refreshes the roster after an overview mutation", () => {
    refreshSpy.mockClear();
    renderInspector();
    fireEvent.click(screen.getByRole("button", { name: "refresh agent" }));
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  it("opens a real tab when ?inspectorTab deep-links in", () => {
    inspectorTabParam = "tools";
    renderInspector();
    expect(screen.getByTestId("tools-tab")).toHaveTextContent("tools:agent_42");
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  // Channel wiring has no equivalent on this runtime — the tab is gone, so a
  // stale deep link lands on Overview like any other unknown value.
  it("falls back to Overview for a retired ?inspectorTab=channels link", () => {
    inspectorTabParam = "channels";
    renderInspector();
    expect(screen.getByTestId("overview-tab")).toHaveTextContent("overview:agent_42");
    expect(screen.queryByRole("tab", { name: "Channels" })).toBeNull();
  });

  it("falls back to Overview for an unknown ?inspectorTab value", () => {
    inspectorTabParam = "bogus";
    renderInspector();
    expect(screen.getByTestId("overview-tab")).toHaveTextContent("overview:agent_42");
  });

  it("mounts the Memory tab on click and via initialTab", () => {
    renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Memory" }));
    expect(screen.getByTestId("memory-tab")).toBeInTheDocument();
    cleanup();
    inspectorTabParam = "memory";
    renderInspector();
    expect(screen.getByTestId("memory-tab")).toBeInTheDocument();
  });

  it("switches the mounted tab when a tab is clicked", () => {
    renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByTestId("tools-tab")).toHaveTextContent("tools:agent_42");
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(screen.getByTestId("instructions-tab")).toHaveTextContent("instructions:agent_42");
    expect(screen.queryByTestId("tools-tab")).not.toBeInTheDocument();
  });

  it("marks the active tab with aria-selected for screen readers", () => {
    renderInspector();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("renders the agent name in the header", () => {
    renderInspector();
    expect(screen.getByText("Scout")).toBeInTheDocument();
  });

  it("shows a calm empty state when no ?agent is present", () => {
    agentParam = null;
    renderInspector();
    expect(screen.getByText(/select an agent/i)).toBeInTheDocument();
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("renders the org controls when the Teams roster has the ?agent id", () => {
    renderInspector();
    expect(screen.getByTestId("org-controls")).toHaveTextContent("org:agent_42");
  });

  it("renders nothing org-side when the Teams roster errors", () => {
    rosterError = new Error("boom");
    rosterData = undefined;
    renderInspector();
    // Overview tab still mounts; the org section degrades to nothing so a
    // pause/resume POST never fires against a missing Paperclip row.
    expect(screen.getByTestId("overview-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });

  it("renders nothing org-side when the agent is absent from the roster", () => {
    rosterData = { agents: [{ id: "someone_else", name: "Other" }] };
    renderInspector();
    expect(screen.getByTestId("overview-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });

  // A Paperclip-native `/<prefix>/agents/<urlKey>` link lands here with the
  // url-key as ?agent, not the uuid (shellUrlFromPaperclipPath has no roster
  // to resolve against). The panel must resolve it against the roster before
  // it reaches any tab or RPC.
  it("resolves a url-key ?agent param to the roster's real agent id", () => {
    agentParam = "cto";
    rosterData = { agents: [{ id: "agent_42", name: "Scout", urlKey: "cto" } as never] };
    renderInspector();
    expect(screen.getByTestId("overview-tab")).toHaveTextContent("overview:agent_42");
    expect(screen.getByTestId("org-controls")).toHaveTextContent("org:agent_42");
    expect(screen.getByText("Scout")).toBeInTheDocument();
  });

  it("keeps the org section on the Overview tab only", () => {
    renderInspector();
    expect(screen.getByTestId("org-controls")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });
});
