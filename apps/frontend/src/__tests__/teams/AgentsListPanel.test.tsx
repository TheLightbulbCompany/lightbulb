// Test for AgentsListPanel.
//
// Security-critical assertion: the create form posts ONLY {name, role}.
// No adapter fields (adapterType, adapterConfig, url, authToken, headers)
// must ever leak from the UI. The BFF whitelists with extra="forbid" so a
// smuggle attempt would 422, but we enforce defense in depth at the UI
// boundary too.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { SWRConfig } from "swr";

interface AgentRow {
  id: string;
  name: string;
  role: string;
  status?: string;
  seeded?: boolean;
  origin?: "Seed" | "Marketplace" | "Custom";
  marketplace?: { listingId: string; slug: string; version: number; seller: string };
  mentionHandle?: string | null;
}
interface AgentsResp {
  // Undefined while loading / on error — the panel's two non-row states.
  data?: { agents: AgentRow[] };
  isLoading: boolean;
  error: unknown;
  mutate: () => void;
}
const mockPost = vi.fn();
const mockMutate = vi.fn();
// Typed with the path arg so a test can assert WHICH key was read.
const mockRead = vi.fn<(path: string) => AgentsResp>(() => ({
  data: { agents: [] },
  isLoading: false,
  error: null,
  mutate: mockMutate,
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

const mockPause = vi.fn().mockResolvedValue(undefined);
const mockResume = vi.fn().mockResolvedValue(undefined);
const mockRemove = vi.fn().mockResolvedValue(undefined);
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({
    setReportsTo: vi.fn(),
    pause: mockPause,
    resume: mockResume,
    remove: mockRemove,
  }),
}));



// The chart view renders the same OrgChart the retired Org chart panel did —
// stubbed here so these tests assert the wiring, not the pan/zoom canvas
// (which OrgChart's own tests cover).
const mockOrgChart = vi.fn();
vi.mock("@/components/teams/org-chart/OrgChart", () => ({
  OrgChart: ({ agents }: { agents: { name: string }[] }) => {
    mockOrgChart(agents);
    return <div data-testid="org-chart">{agents.map((a) => a.name).join(", ")}</div>;
  },
}));

// Team › Message: Lightbulb's DM, or a specialist asked through Lightbulb's DM with its @handle.
// Team cards read the workspace's requests and each profile's skills.
const teamTasks = vi.hoisted(() => ({ list: [] as unknown[] }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ agentNames: new Map([["ceo", "Lightbulb"]]), tasks: { cacheScope: "org", list: async () => ({ tasks: teamTasks.list, next: null }) } }),
}));
const skills = vi.hoisted(() => ({ byAgent: {} as Record<string, unknown> }));
vi.mock("@/hooks/useRuntimeRpc", () => ({ useRuntimeRpc: (_m: string | null, params: { agentId: string }) => ({ data: skills.byAgent[params.agentId] }) }));
const DEFAULT_DIRECTORY = { ceoAgentId: "ceo", participants: [{ kind: "agent", id: "fin", mentionHandle: "finance" }] } as Record<string, unknown>;
const directory = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ directoryProps: directory.props ?? DEFAULT_DIRECTORY }),
}));
const nav = vi.hoisted(() => ({ openChatPanel: vi.fn(), openAgentDmWithDraft: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => nav);

import { AgentsListPanel, derivedHandle } from "@/components/teams/panels/AgentsListPanel";


describe("AgentsListPanel — roster / chart views", () => {
  const roster = [
    { id: "ceo", name: "Friday", role: "ceo" },
    { id: "eng", name: "Forge", role: "engineer" },
  ];

  beforeEach(() => {
    localStorage.clear();
    mockOrgChart.mockClear();
    mockRead.mockReturnValue({
      data: { agents: roster },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
  });

  afterEach(() => {
    mockRead.mockReturnValue({
      data: { agents: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
  });

  it("says so once, calmly, when the roster cannot be read", () => {
    mockRead.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
      mutate: mockMutate,
    });
    render(<AgentsListPanel />);
    expect(screen.getByRole("alert")).toHaveTextContent("We couldn\u2019t load your agents.");
  });
});

describe("Team › Message", () => {
  const agents = { data: { agents: [{ id: "ceo", name: "Lightbulb", role: "ceo" }, { id: "fin", name: "Finance", role: "general" }] }, isLoading: false, error: null, mutate: vi.fn() };
  beforeEach(() => { nav.openChatPanel.mockClear(); nav.openAgentDmWithDraft.mockClear(); mockRead.mockReturnValue(agents); });
  afterEach(() => { mockRead.mockReset(); });

  it("a specialist is asked in Lightbulb's DM (its DM is not a Router room); Lightbulb opens its own DM", () => {
    render(<AgentsListPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Finance through Lightbulb" }));
    expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance ");
    fireEvent.click(screen.getByRole("button", { name: "Ask Lightbulb" }));
    expect(nav.openChatPanel).toHaveBeenCalledWith("ceo");
  });

  it("the front agent reads as Lightbulb, specialists by name only, one Hire a specialist", () => {
    mockRead.mockReturnValue({ ...agents, data: { agents: [{ id: "ceo", name: "CEO", role: "ceo" }, { id: "fin", name: "Finance", role: "cfo" }] } });
    render(<AgentsListPanel />);
    expect(screen.queryByText("CEO")).toBeNull();
    expect(screen.getByRole("button", { name: "Ask Lightbulb" })).toBeInTheDocument();
    expect(screen.queryByText("cfo")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add an AI employee" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Hire a specialist" }));
    expect(screen.getByRole("dialog", { name: "Hire a specialist" })).toBeInTheDocument();
  });

  it("Team sits in the Frame A column with its title row; no Specialist subline; search only from 10 agents (UX slice 6)", () => {
    const roster = (n: number) => [{ id: "ceo", name: "CEO", role: "ceo" }, ...Array.from({ length: n - 1 }, (_, i) => ({ id: `a${i}`, name: `Agent ${i}`, role: "engineer" }))];
    mockRead.mockReturnValue({ ...agents, data: { agents: roster(3) } });
    const { unmount } = render(<AgentsListPanel />);
    const title = screen.getByRole("heading", { level: 1, name: "Team" }).parentElement!;
    expect(title).toHaveClass("frame-doc__title");
    expect(within(title).getByRole("button", { name: "Hire a specialist" })).toBeInTheDocument();
    expect(title.closest(".frame-doc")).not.toBeNull();
    expect(screen.queryByText("Specialist")).toBeNull();
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByLabelText("Search agents")).toBeNull();
    unmount();
    mockRead.mockReturnValue({ ...agents, data: { agents: roster(10) } });
    render(<AgentsListPanel />);
    expect(screen.getByLabelText("Search agents")).toBeInTheDocument();
  });

  it("a card per specialist says what it's for and its recent requests, with Ask; no skill slugs, no org chart", async () => {
    mockRead.mockReturnValue({ ...agents, data: { agents: [
      { id: "fin", name: "Finance", role: "cfo" },
      { id: "ceo", name: "CEO", role: "ceo" },
      { id: "growth", name: "Growth", role: "cmo", title: "Finds you customers" } as AgentRow,
    ] } });
    skills.byAgent = { fin: { skills: [{ name: "stripe", description: "Payments and finance" }, { name: "quickbooks", description: "Finance books" },
      { name: "off", description: "finance", disabled: true }, { name: "canva", description: "Design posters" }] } };
    teamTasks.list = [
      { taskId: "t1", identifier: "t1", title: "Reconcile October", status: "idle", shownGroup: "idle", specialist: "fin", audience: { kind: "room" }, audienceKind: "room", taskRoomId: "tr", watching: false, updatedAt: "2026-11-05T10:00:00Z", version: 1 },
      { taskId: "t0", identifier: "t0", title: "Reconcile October", status: "idle", shownGroup: "idle", specialist: "fin", audience: { kind: "room" }, audienceKind: "room", taskRoomId: "tr", watching: false, updatedAt: "2026-11-04T10:00:00Z", version: 1 },
      { taskId: "t2", identifier: "t2", title: "Plan the week", status: "working", shownGroup: "working", specialist: null, audience: { kind: "room" }, audienceKind: "room", taskRoomId: "tr", watching: false, updatedAt: "2026-11-05T10:00:00Z", version: 1 },
    ];
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><AgentsListPanel /></SWRConfig>);
      const cards = screen.getAllByRole("listitem").filter((li) => li.closest("[aria-label='Specialists']") && li.querySelector("h3"));
      // Lightbulb leads, with one line: never "Coordinator" or its roster "CEO" (W7-7).
      expect(within(cards[0]).getByRole("heading", { name: "Lightbulb" })).toBeInTheDocument();
      expect(within(cards[0]).getByText("Hands your tasks to specialists")).toBeInTheDocument();
      expect(within(cards[0]).queryByText(/Coordinator|CEO/)).toBeNull();
      const fin = cards.find((c) => within(c).queryByRole("heading", { name: "Finance" }))!;
      // No title or persona: no line at all, never its skill slugs (W12-8).
      expect(within(fin).queryByText(/stripe|quickbooks/)).toBeNull();
      expect(within(fin).queryByRole("list", { name: "Finance's skills" })).toBeNull();
      expect(await within(fin).findByRole("button", { name: "Open Reconcile October, Done" })).toBeInTheDocument();
      // The latest of each title, once (W15-11).
      expect(within(fin).getAllByRole("button", { name: /^Open Reconcile October/ })).toHaveLength(1);
      expect(await within(cards[0]).findByRole("button", { name: "Open Plan the week, Working" })).toBeInTheDocument();
      const growth = cards.find((c) => within(c).queryByRole("heading", { name: "Growth" }))!;
      expect(within(growth).getByText("Finds you customers")).toBeInTheDocument();
      expect(within(growth).getByText("No tasks yet.")).toBeInTheDocument();
      // One verb on every card, Lightbulb's too (W12-8).
      expect(within(cards[0]).getByRole("button", { name: "Ask Lightbulb" })).toHaveTextContent("Ask");
      fireEvent.click(within(fin).getByRole("button", { name: "Ask Finance through Lightbulb" }));
      expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance ");
      expect(screen.queryByRole("button", { name: "Org chart view" })).toBeNull();
      expect(screen.queryByTestId("org-chart")).toBeNull();
    } finally { skills.byAgent = {}; teamTasks.list = []; }
  });

  it("W7-7: Ask drafts the @handle when the open room has no specialists; cards never repeat the name", () => {
    directory.props = { ceoAgentId: "ceo", participants: [{ kind: "agent", id: "fin", mentionHandle: null }],
      rooms: [{ participants: [{ kind: "agent", id: "fin", mentionHandle: "finance-2" }] }] };
    mockRead.mockReturnValue({ ...agents, data: { agents: [
      { id: "ceo", name: "CEO", role: "ceo", title: "CEO" } as AgentRow,
      { id: "fin", name: "Finance", role: "general" },
      { id: "eng", name: "Engineering", role: "engineer", title: "Engineering" } as AgentRow,
    ] } });
    skills.byAgent = { eng: { skills: [{ name: "paperclip" }, { name: "codex" }, { name: "hermes-agent" }, { name: "github", description: "Engineering on GitHub" }] } };
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><AgentsListPanel /></SWRConfig>);
      fireEvent.click(screen.getByRole("button", { name: "Ask Finance through Lightbulb" }));
      expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance-2 ");
      // In no room at all: the handle the server derives from the name.
      fireEvent.click(screen.getByRole("button", { name: "Ask Engineering through Lightbulb" }));
      expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@engineering ");
      const eng = screen.getAllByRole("listitem").find((li) => within(li).queryByRole("heading", { name: "Engineering" }))!;
      expect(within(eng).getAllByText("Engineering")).toHaveLength(1);
    } finally { skills.byAgent = {}; directory.props = null; }
  });

  it("the server's mentionHandle wins over a room's and the derived one (GET /teams/agents, #4885)", () => {
    directory.props = { ceoAgentId: "ceo", participants: [], rooms: [{ participants: [{ kind: "agent", id: "fin", mentionHandle: "finance-2" }] }] };
    mockRead.mockReturnValue({ ...agents, data: { agents: [
      { id: "ceo", name: "CEO", role: "ceo" }, { id: "fin", name: "Finance", role: "general", mentionHandle: "finance-3" } as AgentRow,
    ] } });
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><AgentsListPanel /></SWRConfig>);
      fireEvent.click(screen.getByRole("button", { name: "Ask Finance through Lightbulb" }));
      expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance-3 ");
    } finally { directory.props = null; }
  });

  it("derivedHandle matches messaging.py _handle_candidate", () => {
    expect(derivedHandle("Zoë")).toBe("zoe");
    expect(derivedHandle("Growth & Ops")).toBe("growth-ops");
    // Cut at 32 after trimming, like the server (which may leave a trailing "-" the same way).
    expect(derivedHandle("Head of Platform Reliability Engineering East")).toBe("head-of-platform-reliability-eng");
    // Nothing ASCII: the server falls back to agent-<id>, which the client can't know, so no draft handle.
    expect(derivedHandle("!!!")).toBeNull();
    expect(derivedHandle("Lightbulb")).toBeNull();
  });

});
