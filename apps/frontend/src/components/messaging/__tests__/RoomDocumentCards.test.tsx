import { fireEvent, render, screen, within } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }));
const m = vi.hoisted(() => ({ get: vi.fn(), read: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: m.get }) }));
// The viewer's own document read goes through the Teams SWR layer.
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: m.read, download: vi.fn() }) }));
vi.mock("@/components/teams/shared/components/MarkdownView", () => ({
  MarkdownView: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}));

import { RoomDocumentCards } from "../RoomDocumentCards";

const ISSUE = { id: "uuid-12", identifier: "HAB-12", title: "Habits 365 research", status: "done" };
const DOC = {
  id: "doc-1",
  key: "research",
  title: "Habits 365: research & demo video plan",
  body: "# Habits 365: research & demo video plan\n\nHabit-tracking app, 4.7 stars, strongest at streak reminders.",
  updatedAt: new Date().toISOString(),
};

function renderText(text: string) {
  return render(<SWRConfig value={{ provider: () => new Map() }}><RoomDocumentCards text={text} /></SWRConfig>);
}

describe("RoomDocumentCards", () => {
  beforeEach(() => {
    m.get.mockReset();
    m.read.mockReset();
    m.get.mockImplementation((path: string) => Promise.resolve(path.includes("/documents/") ? DOC : ISSUE));
    m.read.mockReturnValue({ data: DOC, isLoading: false, error: undefined });
  });

  it("renders a card with the document title, task, preview and an Open action", async () => {
    renderText("Done: [the plan](/HAB/issues/HAB-12#document-research)");
    const card = await screen.findByRole("button", { name: /Open Habits 365: research & demo video plan, HAB-12/ });
    expect(within(card).getByText("HAB-12 · Habits 365 research")).toBeInTheDocument();
    expect(within(card).getByText(/strongest at streak reminders/)).toBeInTheDocument();
    expect(within(card).getByText(/^Updated /)).toBeInTheDocument();
    expect(m.get).toHaveBeenCalledWith("/teams/issues/HAB-12");
    expect(m.get).toHaveBeenCalledWith("/teams/issues/HAB-12/documents/research");
  });

  it("opens the document in the artifact pane", async () => {
    const { openPanel } = await import("@/components/workspace/workspaceController");
    renderText("https://dev.thelightbulb.company/HAB/issues/HAB-12#document-research");
    fireEvent.click(await screen.findByRole("button", { name: /Open Habits 365/ }));
    expect(openPanel).toHaveBeenCalledWith("artifact", { src: "paperclip", kind: "document", issue: "uuid-12", key: "research", ident: "HAB-12" }, { pushUrl: true });
  });

  it("renders nothing when the task is not readable (another company, deleted)", async () => {
    m.get.mockRejectedValue(new Error("404"));
    const { container } = renderText("/HAB/issues/HAB-99#document-plan");
    await vi.waitFor(() => expect(m.get).toHaveBeenCalled());
    expect(container.querySelector(".room-doc-card")).toBeNull();
  });
});
