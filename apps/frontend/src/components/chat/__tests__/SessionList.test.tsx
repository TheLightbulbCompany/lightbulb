import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SessionList } from "@/components/chat/SessionList";
import type { AgentSession } from "@/hooks/useAgentSessions";

function makeSessions(): AgentSession[] {
  return [
    { sessionId: "11111111-1111-4111-8111-111111111111", key: "k1", label: "Taxes", updatedAt: 2 },
    { sessionId: "22222222-2222-4222-8222-222222222222", key: "k2", label: "Trip plan", updatedAt: 1 },
  ];
}

describe("SessionList", () => {
  it("renders a row per session and a New session control", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText("Taxes")).toBeTruthy();
    expect(screen.getByText("Trip plan")).toBeTruthy();
    expect(screen.getByRole("button", { name: /new session/i })).toBeTruthy();
  });

  it("calls onSelect with the session id when a row is clicked", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Trip plan"));
    expect(onSelect).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });

  it("calls onNew when New session is clicked", () => {
    const onNew = vi.fn();
    render(
      <SessionList sessions={[]} activeSessionId={null} onSelect={vi.fn()} onNew={onNew} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it("marks the active row via aria-current", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId="11111111-1111-4111-8111-111111111111"
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    const active = screen.getByText("Taxes").closest("[aria-current]");
    expect(active?.getAttribute("aria-current")).toBe("true");
  });
});
