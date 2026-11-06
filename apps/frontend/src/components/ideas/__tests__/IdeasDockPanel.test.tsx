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
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging: client, agentNames: new Map() }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => controller);

import { IdeasDockPanel } from "@/components/workspace/panels/IdeasDockPanel";
import { WelcomeIdea } from "../IdeaCard";
import { starterIdeas } from "../HomeIdeas";

const idea = (id: string, agentId: string, agentName: string, kind: Idea["kind"] = "agency", ownerRole = "growth"): Idea => ({
  id, ownerRole, emoji: `e-${id}`, kind, size: kind === "agency" ? "project" : "task", businessTypes: ["any"], title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [`${id} one`, `${id} two`, `${id} three`], ourTurnaround: "about 2 days", yourPart: "about 5 minutes",
  agentId, agentName,
});

const catalog: IdeasResponse = {
  ideas: [idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("c", "eng", "Engineering"),
    idea("d", "growth", "Growth"), idea("e", "fin", "Finance", "everyday")],
};

function renderPanel(params: Record<string, string> = {}) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <IdeasDockPanel id="ideas" params={params} />
  </SWRConfig>);
}

const rowTitles = () => [...document.querySelectorAll(".idea-item__title")].map((el) => el.textContent);
// The catalog rows live under the pane's folded "All ideas" (the welcome card has its own row).
const row = (title: string) => {
  const all = document.querySelector<HTMLElement>(".ideas-pane__all");
  return (all ? within(all) : screen).getByRole("button", { name: title });
};

beforeEach(() => {
  vi.clearAllMocks();
  client.listIdeas.mockResolvedValue(catalog);
  client.startIdea.mockResolvedValue({ roomId: "room-b", projectId: "p-b" });
  client.dismissIdea.mockResolvedValue(undefined);
});

describe("Ideas pane: the list", () => {
  it("shows every idea as one calm row: emoji tile, title, why; no cards, kicker, Start, × or chips", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    expect(rowTitles()).toEqual(["Title a", "Title b", "Title c", "Title d", "Title e"]);
    expect(row("Title a")).toHaveAccessibleDescription("Why a");
    expect(row("Title a").querySelector(".idea-tile--grow")?.textContent).toBe("e-a");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.queryByText(/can do this for you/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Start/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Not now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Teammate" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\$|agency/i);
  });

  it("groups ideas into sections by role, in a fixed order", async () => {
    client.listIdeas.mockResolvedValue({ ideas: [idea("x", "eng", "Engineering", "agency", "engineering"),
      idea("y", "fin", "Finance", "agency", "finance"), idea("z", "ceo", "Friday", "everyday", "ceo"),
      idea("w", "growth", "Growth")] });
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(4));
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Money", "Grow", "Run the day", "Build"]);
    expect(within(screen.getByRole("region", { name: "Money" })).getByRole("button", { name: "Title y" })).toBeInTheDocument();
    expect(rowTitles()).toEqual(["Title y", "Title w", "Title z", "Title x"]);
  });

  it("puts the DM agent's ideas first in a section, then agency jobs, then everyday", async () => {
    renderPanel({ agent: "fin" });
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    expect(rowTitles()).toEqual(["Title e", "Title a", "Title b", "Title c", "Title d"]);
  });

});

describe("Ideas pane: sizes and suggestions", () => {
  it("W7: shows the team's suggestions as Activity does (Do it / Not now), the catalog folded", async () => {
    const suggestion: Idea = { ...idea("s-1", "growth", "Growth"), suggested: true, ourTurnaround: undefined, yourPart: undefined };
    client.listIdeas.mockResolvedValue({ ...catalog, ideas: [suggestion, ...catalog.ideas] });
    renderPanel();
    const suggested = await screen.findByRole("region", { name: "Suggested · 1" });
    const item = within(suggested).getByRole("listitem", { name: "Title s-1" });
    expect(item).toHaveTextContent("Growth would do this");
    expect(within(item).getByRole("button", { name: "Do it" })).toBeInTheDocument();
    // No starter chips beside suggestions; the catalog stays folded under All ideas.
    expect(screen.queryByRole("list", { name: "Starter ideas" })).toBeNull();
    expect(document.querySelector<HTMLDetailsElement>(".ideas-pane__all")!.open).toBe(false);
    await userEvent.click(within(item).getByRole("button", { name: "Not now" }));
    await waitFor(() => expect(client.dismissIdea).toHaveBeenCalledWith("s-1"));
  });

  it("W8: with no suggestions, Activity's starter chips under one lead line; a chip opens its idea", async () => {
    renderPanel();
    const chips = await screen.findByRole("list", { name: "Starter ideas" });
    expect(within(chips).getAllByRole("button").map((b) => b.textContent)).toEqual(starterIdeas(catalog.ideas).map((i) => i.title));
    expect(screen.getByText(/Or start with one of these:/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /^Suggested/ })).toBeNull();
    expect(document.querySelector<HTMLDetailsElement>(".ideas-pane__all")!.open).toBe(false);
    await userEvent.click(within(chips).getByRole("button", { name: "Title b" }));
    expect(screen.getByRole("article", { name: "Title b" })).toBeInTheDocument();
  });
});

describe("Ideas pane: the detail", () => {
  it("opens on a row: title, description, who and how fast, what you get, your part, Let's do it; back returns", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title b"));
    const detail = screen.getByRole("article", { name: "Title b" });
    expect(within(detail).getByRole("heading", { name: "Title b" })).toBeInTheDocument();
    expect(within(detail).getByText("Why b")).toBeInTheDocument();
    const included = within(detail).getByRole("region", { name: "What you get" });
    expect(within(included).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["b one", "b two", "b three"]);
    expect(within(included).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(detail).getByText("Growth takes this · about 2 days")).toBeInTheDocument();
    expect(within(detail).getByText("Your part: about 5 minutes")).toBeInTheDocument();
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

  it("a started idea leaves the list once the server stops listing it (no Running section)", async () => {
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    client.listIdeas.mockResolvedValue({ ...catalog, ideas: catalog.ideas.filter((i) => i.id !== "b") });
    await userEvent.click(row("Title b"));
    await userEvent.click(screen.getByRole("button", { name: "Let's do it" }));
    await waitFor(() => expect(rowTitles()).toEqual(["Title a", "Title c", "Title d", "Title e"]));
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
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

  it("a failed Let's do it keeps the detail open with the reason, and pressing it again works", async () => {
    client.startIdea.mockRejectedValueOnce(new Error("Work did not answer. Try again in a moment."));
    renderPanel();
    await waitFor(() => expect(rowTitles()).toHaveLength(5));
    await userEvent.click(row("Title b"));
    await userEvent.click(screen.getByRole("button", { name: "Let's do it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Work did not answer. Try again in a moment.");
    expect(screen.getByRole("article", { name: "Title b" })).toBeInTheDocument();
    expect(controller.openMessagePanel).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Let's do it" }));
    await waitFor(() => expect(controller.openMessagePanel).toHaveBeenCalledWith("room-b"));
    await waitFor(() => expect(screen.queryByRole("article", { name: "Title b" })).not.toBeInTheDocument());
  });

});

describe("the CEO welcome's proposal", () => {
  const renderWelcome = () =>
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><WelcomeIdea /></SWRConfig>);

  it("is one calm row with See all ideas, no kicker; the row opens that idea in the pane", async () => {
    client.listIdeas.mockResolvedValue(catalog);
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
    client.listIdeas.mockResolvedValue({ ...catalog, ideas: [suggestion, ...catalog.ideas] });
    renderWelcome();
    await waitFor(() => expect(rowTitles()).toEqual(["Title a"]));
  });

  it("is gone once the workspace has started or hidden an idea", async () => {
    for (const data of [{ ...catalog, anyStarted: true }, { ...catalog, anyDismissed: true }]) {
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
