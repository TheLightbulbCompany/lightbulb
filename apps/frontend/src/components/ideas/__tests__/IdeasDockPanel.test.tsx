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
const openMessagePanel = vi.hoisted(() => vi.fn());

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging: client }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => ({ openMessagePanel }));

import { IdeasDockPanel } from "@/components/workspace/panels/IdeasDockPanel";
import { WelcomeIdea } from "../IdeaCard";

const idea = (id: string, agentId: string, agentName: string): Idea => ({
  id, ownerRole: "growth", kind: "agency", businessTypes: ["any"], title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [`${id} one`, `${id} two`, `${id} three`], ourTurnaround: "about 2 days", yourPart: "about 5 minutes",
  agentId, agentName,
});

const catalog: IdeasResponse = {
  ideas: [idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("c", "eng", "Engineering"),
    idea("d", "growth", "Growth"), idea("e", "fin", "Finance")],
  running: [{ ideaId: "z", title: "Running job", agentId: "fin", agentName: "Finance", roomId: "room-z", projectId: "p-z",
    starterId: "u1", starterName: "Mira", createdAt: null, progress: { done: 2, todo: 3, cancelled: 1 } }],
};

function renderPanel(params: Record<string, string> = {}) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <IdeasDockPanel id="ideas" params={params} />
  </SWRConfig>);
}

const cards = () => screen.getAllByRole("article").map((card) => within(card).getByRole("heading").textContent);

beforeEach(() => {
  vi.clearAllMocks();
  client.listIdeas.mockResolvedValue(catalog);
  client.startIdea.mockResolvedValue({ roomId: "room-b", projectId: "p-b" });
  client.dismissIdea.mockResolvedValue(undefined);
});

describe("Ideas pane", () => {
  it("shows a proposal: who, outcome, why, three checks, one effort line, Start, and no price", async () => {
    renderPanel();
    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("Engineering can do this for you")).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: "Title a" })).toBeInTheDocument();
    expect(within(card).getByText("Why a")).toBeInTheDocument();
    expect(within(card).getAllByRole("listitem")).toHaveLength(3);
    expect(within(card).getByText("Your team: about 2 days · You: about 5 minutes")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\$|agency/i);
  });

  it("puts running ideas with progress first, then three proposals, then one-line rows", async () => {
    renderPanel();
    expect(await screen.findByText("Running job")).toBeInTheDocument();
    expect(screen.getByText("Finance · 2 of 5 tasks done")).toBeInTheDocument();
    expect(cards()).toEqual(["Title a", "Title b", "Title c"]);
    expect(screen.getByRole("button", { name: "Start: Title d" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start: Title e" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open: Running job" }));
    expect(openMessagePanel).toHaveBeenCalledWith("room-z");
  });

  it("opened from an agent's DM shows that agent's ideas first", async () => {
    renderPanel({ agent: "growth" });
    await screen.findAllByRole("article");
    expect(cards()).toEqual(["Title b", "Title d", "Title a"]);
  });

  it("filters by teammate", async () => {
    renderPanel();
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "Engineering" }));
    expect(cards()).toEqual(["Title a", "Title c"]);
    expect(screen.queryByText("Running job")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "All" }));
    expect(cards()).toHaveLength(3);
  });

  it("Start calls the API and opens the DM it returns; the pane stays", async () => {
    renderPanel();
    await screen.findAllByRole("article");
    await userEvent.click(within(screen.getAllByRole("article")[1]).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(openMessagePanel).toHaveBeenCalledWith("room-b"));
    expect(client.startIdea).toHaveBeenCalledWith("b");
    expect(client.listIdeas).toHaveBeenCalledTimes(2);
  });

  it("× hides the idea at once and tells the server", async () => {
    let finish: () => void = () => {};
    client.dismissIdea.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    renderPanel();
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "Not now: Title a" }));
    expect(cards()).toEqual(["Title b", "Title c", "Title d"]);
    expect(client.dismissIdea).toHaveBeenCalledWith("a");
    finish();
  });
});

describe("the CEO welcome's proposal", () => {
  it("shows the top idea that is not running and points at the 💡", async () => {
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><WelcomeIdea /></SWRConfig>);
    expect(await screen.findByRole("heading", { name: "Title a" })).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByText(/under the 💡/)).toBeInTheDocument();
  });
});
