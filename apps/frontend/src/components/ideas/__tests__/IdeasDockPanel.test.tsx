import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import type { Idea, IdeasResponse } from "@lightbulb/shared";

const client = vi.hoisted(() => ({
  listIdeas: vi.fn(),
  startIdea: vi.fn(),
  dismissIdea: vi.fn(),
}));
const controller = vi.hoisted(() => ({ openMessagePanel: vi.fn(), openPanel: vi.fn(), updatePanelParams: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging: client }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => controller);

import { IdeasDockPanel } from "@/components/workspace/panels/IdeasDockPanel";
import { WelcomeIdea } from "../IdeaCard";

const idea = (id: string, agentId: string, agentName: string, kind: Idea["kind"] = "agency"): Idea => ({
  id, ownerRole: "growth", kind, size: kind === "agency" ? "project" : "task", businessTypes: ["any"], title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [`${id} one`, `${id} two`, `${id} three`], ourTurnaround: "about 2 days", yourPart: "about 5 minutes",
  agentId, agentName,
});

const catalog: IdeasResponse = {
  ideas: [idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("c", "eng", "Engineering"),
    idea("d", "growth", "Growth"), idea("e", "fin", "Finance", "everyday")],
  running: [{ ideaId: "z", title: "Running job", size: "project", agentId: "fin", agentName: "Finance", roomId: "room-z",
    projectId: "p-z", issueId: null, starterId: "u1", starterName: "Mira", createdAt: null, progress: { done: 2, todo: 3, cancelled: 1 } },
  { ideaId: "y", title: "Teammate job", size: "project", agentId: "eng", agentName: "Engineering", roomId: "room-y",
    projectId: "p-y", issueId: null, starterId: "u2", starterName: "Sam", createdAt: null },
  { ideaId: "x", title: "One errand", size: "task", agentId: "fin", agentName: "Finance", roomId: "room-x",
    projectId: null, issueId: "i-x", starterId: "u1", starterName: "Mira", createdAt: null, progress: { in_progress: 1 } }],
};

function renderPanel(params: Record<string, string> = {}) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <IdeasDockPanel id="ideas" params={params} />
  </SWRConfig>);
}

const rowTitles = () => [...document.querySelectorAll(".idea-item__title")].map((el) => el.textContent);
const row = (title: string) => screen.getByRole("button", { name: title });

beforeEach(() => {
  vi.clearAllMocks();
  client.listIdeas.mockResolvedValue(catalog);
  client.startIdea.mockResolvedValue({ roomId: "room-b", projectId: "p-b" });
  client.dismissIdea.mockResolvedValue(undefined);
});

describe("Ideas pane: the list", () => {
  it("shows every idea as one calm row: avatar, title, why; no cards, kicker, Start, × or chips", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    expect(rowTitles()).toEqual(["Title a", "Title b", "Title c", "Title d", "Title e"]);
    expect(row("Title a")).toHaveAccessibleDescription("Why a");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.queryByText(/can do this for you/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Start/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Not now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Teammate" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\$|agency/i);
  });

  it("puts the DM agent's ideas first, then agency jobs, then everyday", async () => {
    renderPanel({ agent: "fin" });
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    expect(rowTitles()).toEqual(["Title e", "Title a", "Title b", "Title c", "Title d"]);
  });

  it("keeps running ideas at the top; Open only on the viewer's own", async () => {
    renderPanel();
    expect(await screen.findByText("Finance · 2 of 5 tasks done")).toBeInTheDocument();
    expect(screen.getByText("Engineering · Planning · Started by Sam")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open: Teammate job" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open: Running job" }));
    expect(controller.openMessagePanel).toHaveBeenCalledWith("room-z");
  });
});

describe("Ideas pane: sizes and suggestions", () => {
  it("shows a running task with its status, beside a project's tasks done", async () => {
    renderPanel();
    expect(await screen.findByText("Finance · In progress")).toBeInTheDocument();
    expect(screen.getByText("Finance · 2 of 5 tasks done")).toBeInTheDocument();
  });

  it("marks an agent's suggestion For you; its detail has no effort line", async () => {
    const suggestion: Idea = { ...idea("s-1", "growth", "Growth"), suggested: true, ourTurnaround: undefined, yourPart: undefined };
    client.listIdeas.mockResolvedValue({ ...catalog, ideas: [suggestion, ...catalog.ideas] });
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(6));
    expect(row("Title s-1").textContent).toContain("For you");
    expect(row("Title a").textContent).not.toContain("For you");
    await userEvent.click(row("Title s-1"));
    expect(screen.queryByText(/Your team:/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Let's do it" })).toBeInTheDocument();
  });
});

describe("Ideas pane: the detail", () => {
  it("opens on a row: title, description, what's included with checks, effort, Let's do it; back returns", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title b"));
    const detail = screen.getByRole("article", { name: "Title b" });
    expect(within(detail).getByRole("heading", { name: "Title b" })).toBeInTheDocument();
    expect(within(detail).getByText("Why b")).toBeInTheDocument();
    const included = within(detail).getByRole("region", { name: "What's included" });
    expect(within(included).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["b one", "b two", "b three"]);
    expect(within(included).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(detail).getByText("Your team: about 2 days · You: about 5 minutes")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "All ideas" }));
    expect(rowTitles()).toHaveLength(5);
  });

  it("Let's do it starts the idea and opens its DM", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title b"));
    await userEvent.click(screen.getByRole("button", { name: "Let's do it" }));
    await waitFor(() => expect(controller.openMessagePanel).toHaveBeenCalledWith("room-b"));
    expect(client.startIdea).toHaveBeenCalledWith("b");
  });

  it("… → Not interested hides the idea", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title a"));
    await userEvent.click(screen.getByRole("button", { name: "More options" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Not interested" }));
    expect(client.dismissIdea).toHaveBeenCalledWith("a");
    await waitFor(() => expect(rowTitles()).toEqual(["Title b", "Title c", "Title d", "Title e"]));
  });

  it("opens straight on the idea a link names", async () => {
    renderPanel({ idea: "d" });
    expect(await screen.findByRole("article", { name: "Title d" })).toBeInTheDocument();
  });

  it("a second Start of an idea already starting opens its DM without an error", async () => {
    const { notifyApiError } = await import("@/lib/notify");
    client.startIdea.mockRejectedValueOnce(Object.assign(new Error("This idea is already starting."), { status: 409 }));
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title a"));
    client.listIdeas.mockResolvedValue({ ...catalog, running: [...catalog.running,
      { ideaId: "a", title: "Title a", size: "project", agentId: "eng", agentName: "Engineering", roomId: "room-a",
        projectId: "p-a", issueId: null, starterId: "u1", starterName: "Mira", createdAt: null }] });
    await userEvent.click(screen.getByRole("button", { name: "Let's do it" }));
    await waitFor(() => expect(controller.openMessagePanel).toHaveBeenCalledWith("room-a"));
    expect(notifyApiError).not.toHaveBeenCalled();
  });
});

describe("the CEO welcome's proposal", () => {
  const renderWelcome = () =>
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><WelcomeIdea /></SWRConfig>);

  it("is one calm row with See all ideas, no kicker; the row opens that idea in the pane", async () => {
    client.listIdeas.mockResolvedValue({ ...catalog, running: [] });
    renderWelcome();
    await waitFor(() => expect(rowTitles()).toEqual(["Title a"]));
    expect(screen.queryByText(/can do this for you/)).not.toBeInTheDocument();
    await userEvent.click(row("Title a"));
    expect(controller.openPanel).toHaveBeenCalledWith("ideas", { idea: "a" }, { pushUrl: true });
    await userEvent.click(screen.getByRole("button", { name: "See all ideas" }));
    expect(controller.openPanel).toHaveBeenLastCalledWith("ideas", {}, { pushUrl: true });
  });

  it("never proposes an agent's suggestion", async () => {
    const suggestion: Idea = { ...idea("s-1", "growth", "Growth"), suggested: true };
    client.listIdeas.mockResolvedValue({ ...catalog, ideas: [suggestion, ...catalog.ideas], running: [] });
    renderWelcome();
    await waitFor(() => expect(rowTitles()).toEqual(["Title a"]));
  });

  it("is gone once the workspace has started or hidden an idea", async () => {
    for (const data of [catalog, { ...catalog, running: [], anyStarted: true }, { ...catalog, running: [], anyDismissed: true }]) {
      client.listIdeas.mockResolvedValue(data);
      const view = render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <WelcomeIdea /><IdeasDockPanel id="ideas" params={{}} />
      </SWRConfig>);
      await waitFor(() => expect(rowTitles().length).toBeGreaterThan(0));
      expect(screen.queryByRole("button", { name: "See all ideas" })).not.toBeInTheDocument();
      view.unmount();
    }
  });
});
