import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ProgressCard } from "@/hooks/useProgressCard";

const mockUseProgressCard = vi.fn();
vi.mock("@/hooks/useProgressCard", async (orig) => {
  const actual = await orig<typeof import("@/hooks/useProgressCard")>();
  return {
    ...actual,
    useProgressCard: (sessionKey: string | null) => mockUseProgressCard(sessionKey),
  };
});

import { AgentProgressCard } from "@/components/chat/AgentProgressCard";

function card(over: Partial<ProgressCard> = {}): ProgressCard {
  return {
    sessionKey: "agent:agent_a:user_1",
    revision: 2,
    updatedAt: Date.now(),
    steps: [
      { step: "Research the topic", status: "completed" },
      { step: "Draft the report", status: "in_progress" },
      { step: "Send for review", status: "pending" },
    ],
    ...over,
  };
}

beforeEach(() => {
  mockUseProgressCard.mockReset();
});

describe("AgentProgressCard", () => {
  it("renders nothing when there is no card", () => {
    mockUseProgressCard.mockReturnValue({ card: null, isLoading: false, error: undefined });
    const { container } = render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while loading with no card yet", () => {
    mockUseProgressCard.mockReturnValue({ card: null, isLoading: true, error: undefined });
    const { container } = render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("passes the active sessionKey through to the hook", () => {
    mockUseProgressCard.mockReturnValue({ card: null, isLoading: false, error: undefined });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(mockUseProgressCard).toHaveBeenCalledWith("agent:agent_a:user_1");
  });

  it("renders each step with its text and a status glyph", () => {
    mockUseProgressCard.mockReturnValue({ card: card(), isLoading: false, error: undefined });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(screen.getByText("Research the topic")).toBeInTheDocument();
    // "Draft the report" also appears in the collapsed-trigger summary
    // (current step preview), so it's rendered twice.
    expect(screen.getAllByText("Draft the report").length).toBeGreaterThan(0);
    expect(screen.getByText("Send for review")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveAttribute("data-status", "completed");
    expect(items[1]).toHaveAttribute("data-status", "in_progress");
    expect(items[2]).toHaveAttribute("data-status", "pending");
  });

  it("renders the count summary", () => {
    mockUseProgressCard.mockReturnValue({ card: card(), isLoading: false, error: undefined });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(screen.getByText("1/3")).toBeInTheDocument();
  });

  it("renders optional markdown notes", () => {
    mockUseProgressCard.mockReturnValue({
      card: card({ markdown: "**Blocked** on API access" }),
      isLoading: false,
      error: undefined,
    });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText(/on API access/)).toBeInTheDocument();
  });

  it("renders with no steps but markdown present", () => {
    mockUseProgressCard.mockReturnValue({
      card: card({ steps: undefined, markdown: "Just a note" }),
      isLoading: false,
      error: undefined,
    });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    expect(screen.getByText("Just a note")).toBeInTheDocument();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("is collapsible: body hides when the trigger is toggled closed", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    mockUseProgressCard.mockReturnValue({ card: card(), isLoading: false, error: undefined });
    render(<AgentProgressCard sessionKey="agent:agent_a:user_1" />);
    const trigger = screen.getByRole("button", { name: /progress/i });
    expect(screen.getByText("Research the topic")).toBeVisible();
    await userEvent.click(trigger);
    expect(screen.queryByText("Research the topic")).not.toBeInTheDocument();
  });
});
