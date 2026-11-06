// Home's Ideas lane: suggested ideas as cards (title, why, who would do it) with Do it / Not now; with none yet,
// nothing at all.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import type { Idea } from "@lightbulb/shared";

const client = vi.hoisted(() => ({ listIdeas: vi.fn(), startIdea: vi.fn(), dismissIdea: vi.fn() }));
const controller = vi.hoisted(() => ({ openMessagePanel: vi.fn(), openPanel: vi.fn(), openTaskPane: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ messaging: client, agentNames: new Map([["ceo", "Lightbulb"]]) }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => controller);

import { HomeIdeas } from "../HomeIdeas";

const idea = (id: string, over: Partial<Idea> = {}): Idea => ({
  id, kind: "agency", size: "task", businessTypes: ["any"], emoji: "💡", title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [], agentId: "growth", agentName: "Growth", ...over,
});

const lane = () => render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><HomeIdeas /></SWRConfig>);

beforeEach(() => {
  vi.clearAllMocks();
  client.dismissIdea.mockResolvedValue(undefined);
});

describe("HomeIdeas", () => {
  it("a suggested idea says what, why and who; Do it starts a request and opens it", async () => {
    client.listIdeas.mockResolvedValue({ ideas: [idea("s1", { suggested: true }), idea("s2", { suggested: true, agentId: "ceo", agentName: "CEO" }), idea("cat")] });
    client.startIdea.mockResolvedValue({ roomId: "dm-1", taskId: "t-9" });
    lane();
    const card = (await screen.findByRole("heading", { name: "Title s1" })).closest("li")!;
    expect(within(card).getByText("Why s1")).toBeInTheDocument();
    expect(within(card).getByText("Growth")).toBeInTheDocument();
    // The front agent's idea reads as Lightbulb, never its seat name.
    expect(within(screen.getByRole("heading", { name: "Title s2" }).closest("li")!).getByText("Lightbulb")).toBeInTheDocument();
    // Catalog ideas are not in the lane while there are suggestions.
    expect(screen.queryByText("Title cat")).toBeNull();
    expect(screen.getByRole("heading", { name: "Ideas · 2" })).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Do it" }));
    expect(client.startIdea).toHaveBeenCalledWith("s1");
    await waitFor(() => expect(controller.openTaskPane).toHaveBeenCalledWith("t-9"));
    expect(controller.openMessagePanel).not.toHaveBeenCalled();
  });

  it("Not now puts the idea away", async () => {
    client.listIdeas.mockResolvedValue({ ideas: [idea("s1", { suggested: true })] });
    lane();
    await userEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(client.dismissIdea).toHaveBeenCalledWith("s1");
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Title s1" })).toBeNull());
  });

  it("a failed start says so and Do it can be pressed again", async () => {
    client.listIdeas.mockResolvedValue({ ideas: [idea("s1", { suggested: true })] });
    client.startIdea.mockRejectedValue(new Error("This idea is already starting."));
    lane();
    await userEvent.click(await screen.findByRole("button", { name: "Do it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This idea is already starting.");
    expect(screen.getByRole("button", { name: "Do it" })).toBeEnabled();
  });

  it("with no suggestions, the lane is absent: no generic starter chips (W11-6)", async () => {
    client.listIdeas.mockResolvedValue({ ideas: ["a", "b", "c", "d", "e"].map((id) => idea(id)) });
    lane();
    await waitFor(() => expect(client.listIdeas).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("heading", { name: /Ideas/ })).toBeNull();
    expect(screen.queryByText("Title a")).toBeNull();
  });
});
