// Test for AgentsListPanel.
//
// Security-critical assertion: the create form posts ONLY {name, role}.
// No adapter fields (adapterType, adapterConfig, url, authToken, headers)
// must ever leak from the UI. The BFF whitelists with extra="forbid" so a
// smuggle attempt would 422, but we enforce defense in depth at the UI
// boundary too.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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

const mockOpenMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: mockOpenMarketplace }),
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

// Team › Message: Lightbulb's DM, or (switch on) a specialist asked through Lightbulb's DM with its @handle.
const live = vi.hoisted(() => ({ superAgent: false }));
// Team cards (switch on) read the workspace's requests and each profile's skills.
const teamTasks = vi.hoisted(() => ({ list: [] as unknown[] }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useSuperAgent: () => live.superAgent,
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

/** Click through the chooser step ("New agent" → "Build your own"). */
function openBuildForm() {
  const buttons = screen.getAllByRole("button", { name: /new agent/i });
  fireEvent.click(buttons[0]);
  fireEvent.click(screen.getByRole("button", { name: /build your own/i }));
}

describe("AgentsListPanel", () => {
  it("renders empty state when no agents", () => {
    render(<AgentsListPanel />);
    expect(screen.getByRole("heading", { name: "Team" })).toBeInTheDocument();
    expect(screen.getByText("No agents yet.")).toBeInTheDocument();
  });

  it("a search with no match says so once, not beside \"No agents yet.\"", () => {
    render(<AgentsListPanel />);
    fireEvent.change(screen.getByLabelText("Search agents"), { target: { value: "zzz" } });
    expect(screen.getByText("No agents match your search.")).toBeInTheDocument();
    expect(screen.queryByText("No agents yet.")).toBeNull();
  });

  it("create form posts ONLY {name, role} (no adapterType, no URL)", async () => {
    mockPost.mockResolvedValue({ id: "a1" });
    render(<AgentsListPanel />);

    // Two New agent CTAs render when the list is empty (masthead + empty
    // state); openBuildForm clicks the first, then picks "Build your own"
    // on the chooser step.
    openBuildForm();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Helper" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    expect(mockPost).toHaveBeenCalledWith("/agents", {
      name: "Helper",
      role: "engineer",
    });

    // Defense-in-depth: confirm no smuggled fields.
    const [, body] = mockPost.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>).sort()).toEqual([
      "name",
      "role",
    ]);
    expect(body).not.toHaveProperty("adapterType");
    expect(body).not.toHaveProperty("adapterConfig");
    expect(body).not.toHaveProperty("url");
    expect(body).not.toHaveProperty("authToken");
    expect(body).not.toHaveProperty("headers");
  });

  it("rich hire: includes title + reportsTo when the operator fills them", async () => {
    mockPost.mockClear();
    mockPost.mockResolvedValue({ id: "a2" });
    // A roster so the manager picker has a candidate to select. Persistent
    // (not `...Once`) because typing/clicking re-renders the panel, and each
    // re-render re-reads /agents — a one-shot value would revert to empty and
    // drop the "mgr1" <option> before we can select it.
    const emptyResp = { data: { agents: [] }, isLoading: false, error: null, mutate: mockMutate };
    mockRead.mockReturnValue({
      data: { agents: [{ id: "mgr1", name: "Manager Mo", role: "manager" }] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    try {
      render(<AgentsListPanel />);

      openBuildForm();
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Helper" } });
      fireEvent.change(screen.getByLabelText("Title"), {
        target: { value: "Senior engineer" },
      });
      fireEvent.change(screen.getByLabelText("Reports to"), {
        target: { value: "mgr1" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

      await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
      expect(mockPost).toHaveBeenCalledWith("/agents", {
        name: "Helper",
        role: "engineer",
        title: "Senior engineer",
        reportsTo: "mgr1",
      });
      // Still no smuggled adapter fields.
      const [, body] = mockPost.mock.calls[0];
      expect(body).not.toHaveProperty("adapterType");
      expect(body).not.toHaveProperty("adapterConfig");
      expect(body).not.toHaveProperty("url");
    } finally {
      // Restore the module default so later tests' one-shot overrides + re-renders
      // fall back to the empty roster, not this test's persistent one.
      mockRead.mockReturnValue(emptyResp);
    }
  });

  it("rich hire: omits an empty title and 'top level' manager (sends only {name, role})", async () => {
    mockPost.mockClear();
    mockPost.mockResolvedValue({ id: "a3" });
    render(<AgentsListPanel />);
    openBuildForm();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Solo" } });
    // Leave Title blank, leave Reports to on "No manager".
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [, body] = mockPost.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>).sort()).toEqual(["name", "role"]);
  });

  it("chooser hire path opens the marketplace drawer and closes the dialog", () => {
    mockPost.mockClear();
    mockOpenMarketplace.mockClear();
    render(<AgentsListPanel />);

    const buttons = screen.getAllByRole("button", { name: /new agent/i });
    fireEvent.click(buttons[0]);
    fireEvent.click(screen.getByRole("button", { name: /hire from marketplace/i }));

    expect(mockOpenMarketplace).toHaveBeenCalledWith({ view: "browse" });
    // Dialog closed: the build form never rendered, nothing was posted.
    expect(screen.queryByLabelText("Name")).toBeNull();
    expect(mockPost).not.toHaveBeenCalled();
  });

  // The rail's Hire button moved here: hiring is the Agents tab's verb.
  it("the header's Add an AI employee opens the marketplace drawer", () => {
    mockOpenMarketplace.mockClear();
    render(<AgentsListPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Add an AI employee" }));
    expect(mockOpenMarketplace).toHaveBeenCalledWith({ view: "browse" });
  });

  it("renders agent rows with link to /teams/agents/{id}", () => {
    mockRead.mockReturnValueOnce({
      data: { agents: [{ id: "a1", name: "Alice", role: "ceo" }] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    render(<AgentsListPanel />);
    expect(screen.getByText("Alice")).toBeInTheDocument();
    const link = screen.getByText("Open →");
    expect(link.closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=agent-inspector&agent=a1",
    );
  });

  it("renders marketplace origin and canonical version", () => {
    mockRead.mockReturnValueOnce({
      data: {
        agents: [
          {
            id: "forge",
            name: "Forge",
            role: "engineer",
            origin: "Marketplace",
            marketplace: { listingId: "listing-1", slug: "forge", version: 3, seller: "seller-1" },
          },
        ],
      },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    render(<AgentsListPanel />);
    expect(screen.getByText("Marketplace · forge v3")).toBeInTheDocument();
  });

  it("row pause and delete actions remain available for starter agents", async () => {
    mockPause.mockClear();
    mockRead.mockReturnValueOnce({
      data: { agents: [{ id: "f", name: "Friday", role: "ceo", status: "idle", seeded: true }] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    render(<AgentsListPanel />);
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /pause/i }));
    await waitFor(() => expect(mockPause).toHaveBeenCalledWith("f"));
  });

  it("shows delete for a non-seeded agent even if its role is ceo", () => {
    mockRead.mockReturnValueOnce({
      // A user-created agent that happens to use role "ceo" is NOT seeded.
      data: { agents: [{ id: "u", name: "Faux", role: "ceo", status: "idle" }] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    render(<AgentsListPanel />);
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
  });
});

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

  it("opens on the roster and reads /agents exactly once for both views", () => {
    render(<AgentsListPanel />);
    expect(mockRead).toHaveBeenCalledWith("/agents");
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.queryByTestId("org-chart")).toBeNull();
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("switches to the chart over the roster it already has, and remembers the pick", () => {
    const { unmount } = render(<AgentsListPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Org chart view" }));
    expect(screen.getByTestId("org-chart")).toHaveTextContent("Friday, Forge");
    expect(mockOrgChart).toHaveBeenCalledWith(roster);
    // One SWR key for both views: the chart draws the roster the panel already
    // has rather than adding a second /agents read of its own.
    expect(new Set(mockRead.mock.calls.map((c) => c[0]))).toEqual(new Set(["/agents"]));
    unmount();

    // Persisted per browser (the tasksViewMode idiom), so a remount opens on it.
    render(<AgentsListPanel />);
    expect(screen.getByTestId("org-chart")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Org chart view" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("search narrows the LIST, and the chart offers no search box", () => {
    // An org chart is a structure, and a filtered structure is not a smaller
    // structure — it is several orphaned roots that misdescribe who reports to
    // whom. So search stays a list affordance, the way grouping and sort are
    // list-only on Tasks, and the chart draws the whole roster.
    render(<AgentsListPanel />);
    fireEvent.change(screen.getByLabelText("Search agents"), { target: { value: "forge" } });
    expect(screen.getByText("Forge")).toBeInTheDocument();
    expect(screen.queryByText("Friday")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Org chart view" }));
    expect(screen.queryByLabelText("Search agents")).toBeNull();
    expect(screen.getByTestId("org-chart")).toHaveTextContent("Forge");
    expect(screen.getByTestId("org-chart")).toHaveTextContent("Friday");
  });

  it("keeps the search term for the list when returning from the chart", () => {
    render(<AgentsListPanel />);
    fireEvent.change(screen.getByLabelText("Search agents"), { target: { value: "forge" } });
    fireEvent.click(screen.getByRole("button", { name: "Org chart view" }));
    fireEvent.click(screen.getByRole("button", { name: "List view" }));
    expect(screen.getByLabelText("Search agents")).toHaveValue("forge");
    expect(screen.queryByText("Friday")).toBeNull();
  });

  it("initialView='chart' (the retired ?panel=teams-org-chart link) opens the chart without changing the preference", () => {
    const { unmount } = render(<AgentsListPanel initialView="chart" />);
    expect(screen.getByTestId("org-chart")).toBeInTheDocument();
    expect(localStorage.getItem("paperclip:agents:view-mode")).toBeNull();
    unmount();
    // A plain Agents open clears the param, so the stored default (list) wins.
    render(<AgentsListPanel />);
    expect(screen.queryByTestId("org-chart")).toBeNull();
    expect(screen.getByText("Friday")).toBeInTheDocument();
  });

  it("an explicit pick beats the deep link and becomes the preference", () => {
    render(<AgentsListPanel initialView="chart" />);
    fireEvent.click(screen.getByRole("button", { name: "List view" }));
    expect(screen.queryByTestId("org-chart")).toBeNull();
    expect(localStorage.getItem("paperclip:agents:view-mode")).toBe("list");
  });

  it("re-delivering the param on an already-open panel re-applies the view", () => {
    const { rerender } = render(<AgentsListPanel />);
    expect(screen.queryByTestId("org-chart")).toBeNull();
    rerender(<AgentsListPanel initialView="chart" />);
    expect(screen.getByTestId("org-chart")).toBeInTheDocument();
    // ...and a plain nav (param cleared) drops back to the stored view.
    rerender(<AgentsListPanel />);
    expect(screen.queryByTestId("org-chart")).toBeNull();
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
  afterEach(() => { live.superAgent = false; mockRead.mockReset(); });

  it("switch on: a specialist is asked in Lightbulb's DM (its DM is not a Router room); Lightbulb opens its own DM", () => {
    live.superAgent = true;
    render(<AgentsListPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Finance through Lightbulb" }));
    expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance ");
    fireEvent.click(screen.getByRole("button", { name: "Message Lightbulb" }));
    expect(nav.openChatPanel).toHaveBeenCalledWith("ceo");
  });

  it("switch on: the front agent reads as Lightbulb, specialists by name only, one Hire a specialist", () => {
    live.superAgent = true;
    mockRead.mockReturnValue({ ...agents, data: { agents: [{ id: "ceo", name: "CEO", role: "ceo" }, { id: "fin", name: "Finance", role: "cfo" }] } });
    render(<AgentsListPanel />);
    expect(screen.queryByText("CEO")).toBeNull();
    expect(screen.getByRole("button", { name: "Message Lightbulb" })).toBeInTheDocument();
    expect(screen.queryByText("cfo")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add an AI employee" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Hire a specialist" }));
    expect(screen.getByRole("dialog", { name: "Hire a specialist" })).toBeInTheDocument();
  });

  it("switch on: a card per specialist says what it's for, its skills and recent requests, with Ask; no org chart", async () => {
    live.superAgent = true;
    mockRead.mockReturnValue({ ...agents, data: { agents: [
      { id: "fin", name: "Finance", role: "cfo" },
      { id: "ceo", name: "CEO", role: "ceo" },
      { id: "growth", name: "Growth", role: "cmo", title: "Finds you customers" } as AgentRow,
    ] } });
    skills.byAgent = { fin: { skills: [{ name: "stripe", description: "Payments and finance" }, { name: "quickbooks", description: "Finance books" },
      { name: "off", description: "finance", disabled: true }, { name: "canva", description: "Design posters" }] } };
    teamTasks.list = [
      { taskId: "t1", identifier: "t1", title: "Reconcile October", status: "idle", shownGroup: "idle", specialist: "fin", audience: { kind: "room" }, audienceKind: "room", taskRoomId: "tr", watching: false, updatedAt: "2026-11-05T10:00:00Z", version: 1 },
      { taskId: "t2", identifier: "t2", title: "Plan the week", status: "working", shownGroup: "working", specialist: null, audience: { kind: "room" }, audienceKind: "room", taskRoomId: "tr", watching: false, updatedAt: "2026-11-05T10:00:00Z", version: 1 },
    ];
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><AgentsListPanel /></SWRConfig>);
      const cards = screen.getAllByRole("listitem").filter((li) => li.closest("[aria-label='Specialists']") && li.querySelector("h3"));
      // Lightbulb leads, with one line: never "Coordinator" or its roster "CEO" (W7-7).
      expect(within(cards[0]).getByRole("heading", { name: "Lightbulb" })).toBeInTheDocument();
      expect(within(cards[0]).getByText("Coordinates your requests")).toBeInTheDocument();
      expect(within(cards[0]).queryByText(/Coordinator|CEO/)).toBeNull();
      const fin = cards.find((c) => within(c).queryByRole("heading", { name: "Finance" }))!;
      // No title or persona: the line comes from its skills; disabled skills don't show.
      expect(within(fin).getByText("Works with stripe and quickbooks.")).toBeInTheDocument();
      expect(within(fin).getByRole("list", { name: "Finance's skills" })).toHaveTextContent("stripequickbooks");
      expect(await within(fin).findByRole("button", { name: "Open Reconcile October, Done" })).toBeInTheDocument();
      expect(await within(cards[0]).findByRole("button", { name: "Open Plan the week, Working" })).toBeInTheDocument();
      const growth = cards.find((c) => within(c).queryByRole("heading", { name: "Growth" }))!;
      expect(within(growth).getByText("Finds you customers")).toBeInTheDocument();
      expect(within(growth).getByText("No requests yet.")).toBeInTheDocument();
      fireEvent.click(within(fin).getByRole("button", { name: "Ask Finance through Lightbulb" }));
      expect(nav.openAgentDmWithDraft).toHaveBeenCalledWith("ceo", "@finance ");
      expect(screen.queryByRole("button", { name: "Org chart view" })).toBeNull();
      expect(screen.queryByTestId("org-chart")).toBeNull();
    } finally { skills.byAgent = {}; teamTasks.list = []; }
  });

  it("W7-7: Ask drafts the @handle when the open room has no specialists; cards hide plumbing skills and never repeat the name", () => {
    live.superAgent = true;
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
      expect(within(eng).getByRole("list", { name: "Engineering's skills" })).toHaveTextContent(/^github$/);
      expect(within(eng).getByText("Works with github.")).toBeInTheDocument();
      expect(within(eng).getAllByText("Engineering")).toHaveLength(1);
    } finally { skills.byAgent = {}; directory.props = null; }
  });

  it("W8-7: each card shows its own top skills from the same enabled set, never the same tags and +N more", () => {
    live.superAgent = true;
    mockRead.mockReturnValue({ ...agents, data: { agents: [
      { id: "ceo", name: "CEO", role: "ceo" },
      { id: "growth", name: "Growth", role: "cmo", capabilities: "Finds and wins customers; owns marketing and outreach." } as AgentRow,
      { id: "fin", name: "Finance", role: "cfo", capabilities: "Keeps the books and pays invoices." } as AgentRow,
    ] } });
    const same = { skills: [
      { name: "ascii-video", description: "Render ASCII video" }, { name: "seo-audit", description: "Marketing site audit" },
      { name: "cold-email", description: "Outreach to customers" }, { name: "invoice-pay", description: "Pay invoices" },
      { name: "bookkeeping", description: "Keep the books" },
    ] };
    skills.byAgent = { ceo: same, growth: same, fin: same };
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><AgentsListPanel /></SWRConfig>);
      const card = (name: string) => screen.getAllByRole("listitem").find((li) => within(li).queryByRole("heading", { name }))!;
      expect(within(card("Growth")).getByRole("list", { name: "Growth's skills" })).toHaveTextContent(/^(seo-audit|cold-email){2}$/);
      expect(within(card("Finance")).getByRole("list", { name: "Finance's skills" })).toHaveTextContent(/^(invoice-pay|bookkeeping){2}$/);
      expect(within(card("Lightbulb")).queryByRole("list", { name: "Lightbulb's skills" })).toBeNull();
      expect(screen.queryByText(/more$/)).toBeNull();
    } finally { skills.byAgent = {}; }
  });

  it("the server's mentionHandle wins over a room's and the derived one (GET /teams/agents, #4885)", () => {
    live.superAgent = true;
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

  it("switch off: every agent's Message opens its DM", () => {
    render(<AgentsListPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Message Finance" }));
    expect(nav.openChatPanel).toHaveBeenCalledWith("fin");
    expect(nav.openAgentDmWithDraft).not.toHaveBeenCalled();
  });
});
