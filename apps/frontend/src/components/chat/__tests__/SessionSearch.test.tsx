// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { SessionSearchHit } from "@/hooks/useSessionSearch";

// SessionSearch is presentational over useSessionSearch — mock the hook so
// these tests drive its exact return shape without a real debounce/RPC.
const useSessionSearch = vi.fn();
vi.mock("@/hooks/useSessionSearch", () => ({
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
}));

const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyApiError: (...args: unknown[]) => notifyApiError(...args),
}));

import { SessionSearch } from "../SessionSearch";
import type { AgentSession } from "@/hooks/useAgentSessions";

function idleState() {
  return {
    isActive: false,
    effectiveQuery: "",
    results: [] as SessionSearchHit[],
    isLoading: false,
    indexing: false,
    truncated: false,
    error: undefined,
  };
}

function hit(overrides: Partial<SessionSearchHit> = {}): SessionSearchHit {
  return {
    sessionKey: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
    sessionId: "11111111-1111-4111-8111-111111111111",
    messageId: "msg-1",
    role: "user",
    timestamp: Date.now() - 5000,
    snippet: "a question about my taxes this year",
    score: 0.5,
    ...overrides,
  };
}

const noSessions: AgentSession[] = [];

describe("SessionSearch", () => {
  beforeEach(() => {
    useSessionSearch.mockReset().mockReturnValue(idleState());
    notifyApiError.mockClear();
  });

  it("renders a search input", () => {
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByRole("searchbox", { name: /search conversations/i })).toBeInTheDocument();
  });

  it("shows nothing below the box while the query is inactive (below the min length)", () => {
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.queryByText(/searching/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no matches/i)).not.toBeInTheDocument();
  });

  it("shows a loading hint while a search is in flight", () => {
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true, isLoading: true });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByText(/searching/i)).toBeInTheDocument();
  });

  it("shows an indexing hint when the gateway reports a first-use reconcile", () => {
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true, indexing: true });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByText(/indexing/i)).toBeInTheDocument();
  });

  it("shows a no-matches hint for an active search with zero results", () => {
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByText(/no matches/i)).toBeInTheDocument();
  });

  it("shows a truncated hint when the gateway capped the response", () => {
    useSessionSearch.mockReturnValue({
      ...idleState(),
      isActive: true,
      results: [hit()],
      truncated: true,
    });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByText(/showing first/i)).toBeInTheDocument();
  });

  it("groups multiple hits from the same session under one heading", () => {
    useSessionSearch.mockReturnValue({
      ...idleState(),
      isActive: true,
      results: [
        hit({ messageId: "msg-1", snippet: "first taxes question" }),
        hit({ messageId: "msg-2", snippet: "second taxes question" }),
        hit({
          messageId: "msg-3",
          sessionKey: "agent:agent-x:user-1:22222222-2222-4222-8222-222222222222",
          sessionId: "22222222-2222-4222-8222-222222222222",
          snippet: "unrelated session",
        }),
      ],
    });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    // Two session groups, one of which has two hits.
    expect(screen.getByText(/first taxes question/i)).toBeInTheDocument();
    expect(screen.getByText(/second taxes question/i)).toBeInTheDocument();
    expect(screen.getByText(/unrelated session/i)).toBeInTheDocument();
  });

  it("uses the known session's label when the agent's session list has it", () => {
    const sessions: AgentSession[] = [
      {
        sessionId: "11111111-1111-4111-8111-111111111111",
        key: "agent:agent-x:user-1:11111111-1111-4111-8111-111111111111",
        label: "Taxes 2026",
        rawLabel: "Taxes 2026",
      },
    ];
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true, results: [hit()] });
    render(<SessionSearch agentId="agent-x" sessions={sessions} onSelect={vi.fn()} />);
    expect(screen.getByText("Taxes 2026")).toBeInTheDocument();
  });

  it("falls back to General for a hit whose session key has no trailing UUID", () => {
    useSessionSearch.mockReturnValue({
      ...idleState(),
      isActive: true,
      results: [hit({ sessionKey: "agent:agent-x:user-1", sessionId: "irrelevant-internal-id" })],
    });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(screen.getByText("General")).toBeInTheDocument();
  });

  it("clicking a hit calls onSelect with the resolved sessionId and the hit's messageId", () => {
    const onSelect = vi.fn();
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true, results: [hit()] });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /taxes/i }));
    expect(onSelect).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111", "msg-1");
  });

  it("clicking a General hit (3-part key) calls onSelect with a null sessionId", () => {
    const onSelect = vi.fn();
    useSessionSearch.mockReturnValue({
      ...idleState(),
      isActive: true,
      results: [hit({ sessionKey: "agent:agent-x:user-1", messageId: "msg-general" })],
    });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /taxes/i }));
    expect(onSelect).toHaveBeenCalledWith(null, "msg-general");
  });

  it("renders the snippet as text, never as injected HTML", () => {
    useSessionSearch.mockReturnValue({
      ...idleState(),
      isActive: true,
      effectiveQuery: "taxes",
      results: [hit({ snippet: "<b>taxes</b> and <script>alert(1)</script>" })],
    });
    const { container } = render(
      <SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<b>taxes</b>");
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("notifies on an RPC error", () => {
    const err = new Error("gateway unavailable");
    useSessionSearch.mockReturnValue({ ...idleState(), isActive: true, error: err });
    render(<SessionSearch agentId="agent-x" sessions={noSessions} onSelect={vi.fn()} />);
    expect(notifyApiError).toHaveBeenCalledWith(err, expect.any(String), expect.anything());
  });
});
