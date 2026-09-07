import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { SessionList } from "@/components/chat/SessionList";
import type { AgentSession } from "@/hooks/useAgentSessions";

function makeSessions(): AgentSession[] {
  return [
    { sessionId: "11111111-1111-4111-8111-111111111111", key: "k1", label: "Taxes", rawLabel: "Taxes", updatedAt: 2 },
    { sessionId: "22222222-2222-4222-8222-222222222222", key: "k2", label: "Trip plan", rawLabel: "Trip plan", updatedAt: 1 },
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

  it("renders the legacy row and selects null when clicked", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={[{ sessionId: null, key: "agent:a:u", label: "Agent updates", rawLabel: "Agent updates" }]}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Agent updates"));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("shows a placeholder row when the active session has no sessions.list row yet", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId="99999999-9999-4999-8999-999999999999"
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    // The placeholder is the aria-current row; the "+ New session" action is not.
    const placeholder = screen.getByRole("button", { name: "New session", current: true });
    expect(placeholder).toBeTruthy();
  });

  it("does not show the placeholder when the active session is materialized", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId="11111111-1111-4111-8111-111111111111"
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "New session" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("aria-current")).toBeNull();
  });

  it("renames a named session via the pencil → input → Enter flow", () => {
    const onRename = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={onRename}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Rename Taxes" }));
    const input = screen.getByRole("textbox", { name: "Rename session Taxes" });
    fireEvent.change(input, { target: { value: "Taxes 2026" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "11111111-1111-4111-8111-111111111111" }),
      "Taxes 2026",
    );
  });

  it("offers no rename affordance on the legacy row", () => {
    render(
      <SessionList
        sessions={[{ sessionId: null, key: "agent:a:u", label: "Agent updates", rawLabel: "Agent updates" }]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Rename Agent updates" })).toBeNull();
  });

  it("asks to delete a named session via the trash affordance", () => {
    const onDelete = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onDelete={onDelete}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete Taxes" }));
    expect(onDelete).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "11111111-1111-4111-8111-111111111111", key: "k1" }),
    );
  });

  it("delete asks the caller — it never selects the row it sits on", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete Taxes" }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("offers no delete affordance on the legacy row", () => {
    render(
      <SessionList
        sessions={[{ sessionId: null, key: "agent:a:u", label: "Agent updates", rawLabel: "Agent updates" }]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Delete Agent updates" })).toBeNull();
  });

  it("Enter followed by the unmount blur commits exactly once", () => {
    const onRename = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={onRename}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Rename Taxes" }));
    const input = screen.getByRole("textbox", { name: "Rename session Taxes" });
    fireEvent.change(input, { target: { value: "Taxes 2026" } });
    // Real browsers fire a blur when the focused input unmounts on commit; both
    // handlers then run against the SAME pre-close closure. Dispatching both in
    // one act batch reproduces that: state updates don't flush between them.
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(onRename).toHaveBeenCalledTimes(1);
  });

  it("the blur after Escape does not commit the canceled value", () => {
    const onRename = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={onRename}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Rename Taxes" }));
    const input = screen.getByRole("textbox", { name: "Rename session Taxes" });
    fireEvent.change(input, { target: { value: "canceled edit" } });
    // Same stale-closure shape as the Enter case: the blur that follows the
    // Escape-triggered unmount must not commit the value the user canceled.
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(onRename).not.toHaveBeenCalled();
  });

  it("seeds the rename editor from the UNTRUNCATED title, not the display label", () => {
    const longTitle = "a".repeat(60);
    const onRename = vi.fn();
    render(
      <SessionList
        sessions={[
          {
            sessionId: "33333333-3333-4333-8333-333333333333",
            key: "k3",
            label: `${longTitle.slice(0, 48)}…`,
            rawLabel: longTitle,
          },
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={onRename}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^Rename/ }));
    const input = screen.getByRole("textbox");
    // Editing must start from the full title — committing the truncated
    // display copy would persist the ellipsis'd string as the real name.
    expect((input as HTMLInputElement).value).toBe(longTitle);
    // And saving the unchanged full title is a no-op, not a rename.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).not.toHaveBeenCalled();
  });

  it("shows a relative timestamp", () => {
    render(
      <SessionList
        sessions={[
          {
            sessionId: "44444444-4444-4444-8444-444444444444",
            key: "k4",
            label: "Q3 pricing",
            rawLabel: "Q3 pricing",
            updatedAt: Date.now() - 7_200_000,
          },
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText("2h ago")).toBeTruthy();
  });

  it("shows a one-line preview when present", () => {
    render(
      <SessionList
        sessions={[
          {
            sessionId: "44444444-4444-4444-8444-444444444444",
            key: "k4",
            label: "Q3 pricing",
            rawLabel: "Q3 pricing",
            preview: "I checked the numbers",
          },
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText(/I checked the numbers/)).toBeTruthy();
  });

  it("renders cleanly with neither a preview nor a timestamp", () => {
    render(
      <SessionList
        sessions={[
          {
            sessionId: "44444444-4444-4444-8444-444444444444",
            key: "k4",
            label: "Q3 pricing",
            rawLabel: "Q3 pricing",
          },
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText("Q3 pricing")).toBeTruthy();
    // No empty <time> element left behind when there is nothing to say.
    expect(document.querySelector("time")).toBeNull();
  });

  it("the whole row stays one click target — the preview selects too", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={[
          {
            sessionId: "44444444-4444-4444-8444-444444444444",
            key: "k4",
            label: "Q3 pricing",
            rawLabel: "Q3 pricing",
            preview: "I checked the numbers",
          },
        ]}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText(/I checked the numbers/));
    expect(onSelect).toHaveBeenCalledWith("44444444-4444-4444-8444-444444444444");
  });

  it("marks a failed run visibly, and says so in text not just colour", () => {
    render(
      <SessionList
        sessions={[
          {
            sessionId: "44444444-4444-4444-8444-444444444444",
            key: "k4",
            label: "Nightly CEO",
            rawLabel: "Nightly CEO",
            status: "failed",
            lastRunError: "bedrock 429",
          },
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    const marker = screen.getByLabelText(/failed/i);
    expect(marker).toBeTruthy();
    expect(marker.getAttribute("title")).toContain("bedrock 429");
  });

  it("marks killed and timed-out runs too", () => {
    for (const status of ["killed", "timeout"] as const) {
      const { unmount } = render(
        <SessionList
          sessions={[
            {
              sessionId: "44444444-4444-4444-8444-444444444444",
              key: "k4",
              label: "Nightly CEO",
              rawLabel: "Nightly CEO",
              status,
            },
          ]}
          activeSessionId={null}
          onSelect={vi.fn()}
          onNew={vi.fn()}
        />,
      );
      expect(screen.getByLabelText(/failed|stopped|timed out/i)).toBeTruthy();
      unmount();
    }
  });

  it("shows nothing extra for a successful or still-running row", () => {
    for (const status of ["done", "running", "queued", undefined] as const) {
      const { unmount } = render(
        <SessionList
          sessions={[
            {
              sessionId: "44444444-4444-4444-8444-444444444444",
              key: "k4",
              label: "Nightly CEO",
              rawLabel: "Nightly CEO",
              status,
            },
          ]}
          activeSessionId={null}
          onSelect={vi.fn()}
          onNew={vi.fn()}
        />,
      );
      expect(screen.queryByLabelText(/failed|stopped|timed out/i)).toBeNull();
      unmount();
    }
  });

  it("escape cancels a rename without committing", () => {
    const onRename = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        onRename={onRename}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Rename Taxes" }));
    const input = screen.getByRole("textbox", { name: "Rename session Taxes" });
    fireEvent.change(input, { target: { value: "nope" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
