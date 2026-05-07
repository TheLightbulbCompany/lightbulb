// Test for AgentsListPanel.
//
// Security-critical assertion: the create form posts ONLY {name, role}.
// No adapter fields (adapterType, adapterConfig, url, authToken, headers)
// must ever leak from the UI. The BFF whitelists with extra="forbid" so a
// smuggle attempt would 422, but we enforce defense in depth at the UI
// boundary too.
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

interface AgentRow {
  id: string;
  name: string;
  role: string;
}
interface AgentsResp {
  data: { agents: AgentRow[] };
  isLoading: boolean;
  error: unknown;
  mutate: () => void;
}
const mockPost = vi.fn();
const mockMutate = vi.fn();
const mockRead = vi.fn<() => AgentsResp>(() => ({
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

import { AgentsListPanel } from "@/components/teams/panels/AgentsListPanel";

describe("AgentsListPanel", () => {
  it("renders empty state when no agents", () => {
    render(<AgentsListPanel />);
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("No agents yet.")).toBeInTheDocument();
  });

  it("create form posts ONLY {name, role} (no adapterType, no URL)", async () => {
    mockPost.mockResolvedValue({ id: "a1" });
    render(<AgentsListPanel />);

    // Two New agent CTAs render when the list is empty (masthead + empty
    // state). Click the first.
    const buttons = screen.getAllByRole("button", { name: /new agent/i });
    fireEvent.click(buttons[0]);
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
    expect(link.closest("a")).toHaveAttribute("href", "/teams/agents/a1");
  });
});
