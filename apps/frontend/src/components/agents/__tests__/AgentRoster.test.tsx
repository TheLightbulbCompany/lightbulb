// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

vi.mock("@/components/agents/AgentIdentity", () => ({
  Monogram: () => <span data-testid="monogram" />,
}));
vi.mock("@/components/teams/shared/components/LiveRunDot", () => ({
  LiveRunDot: () => <span data-testid="live-run-dot" />,
}));
vi.mock("@/lib/agentDesign", () => ({ fromInstalledAgent: (a: unknown) => a }));
vi.mock("@/lib/modelDisplay", () => ({
  buildModelNameMap: () => ({}),
  displayModelName: (m: string | null) => m || null,
}));

import { AgentRoster, orderRoster, rosterStateFor, type RosterEntry } from "../AgentRoster";
import type { Agent } from "@/hooks/useAgents";

const agent = (id: string, name: string, model?: string): Agent => ({ id, name, model });

function entry(
  id: string,
  name: string,
  state: RosterEntry["state"],
  isCeo = false,
  model?: string,
): RosterEntry {
  return { agent: agent(id, name, model), state, isCeo };
}

function renderRoster(entries: RosterEntry[], props: Partial<Parameters<typeof AgentRoster>[0]> = {}) {
  const handlers = {
    onSelect: vi.fn(),
    onOpenWork: vi.fn(),
    onInspect: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  };
  render(
    <AgentRoster
      entries={entries}
      activeAgentId={null}
      publishedAgentIds={new Set<string>()}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

const rowNames = () =>
  Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map((n) => n.textContent);

describe("rosterStateFor", () => {
  it("maps the Teams BFF status vocabulary onto roster states", () => {
    expect(rosterStateFor("running")).toBe("running");
    expect(rosterStateFor("error")).toBe("attention");
    expect(rosterStateFor("paused")).toBe("paused");
    expect(rosterStateFor("terminated")).toBe("paused");
    expect(rosterStateFor("idle")).toBe("idle");
  });

  it("degrades an unknown or missing status to idle, never to a louder state", () => {
    // The BFF forwards upstream Paperclip JSON verbatim and the enum has
    // drifted before, so a value we don't recognise must not invent alarm.
    expect(rosterStateFor("compacting")).toBe("idle");
    expect(rosterStateFor(null)).toBe("idle");
    expect(rosterStateFor(undefined)).toBe("idle");
  });
});

describe("orderRoster", () => {
  it("pins the CEO first even when other agents need attention", () => {
    const ordered = orderRoster([
      entry("a", "Zeta", "attention"),
      entry("f", "Friday", "idle", true),
      entry("b", "Alpha", "running"),
    ]);
    expect(ordered.map((e) => e.agent.id)).toEqual(["f", "a", "b"]);
  });

  it("hoists agents that need attention and sinks paused ones", () => {
    const ordered = orderRoster([
      entry("p", "Paused One", "paused"),
      entry("i", "Idle One", "idle"),
      entry("e", "Errored One", "attention"),
    ]);
    expect(ordered.map((e) => e.agent.id)).toEqual(["e", "i", "p"]);
  });

  it("does NOT hoist a running agent — a transient run must not move the row", () => {
    // The whole roster fits on screen at the 10-agent cap, so promoting live
    // work buys no visibility and costs the owner their spatial memory.
    const ordered = orderRoster([
      entry("z", "Zoe", "running"),
      entry("a", "Ada", "idle"),
    ]);
    expect(ordered.map((e) => e.agent.id)).toEqual(["a", "z"]);
  });

  it("breaks ties by display name so the order is stable, not input-order", () => {
    const ordered = orderRoster([
      entry("c", "Charlie", "idle"),
      entry("a", "Alpha", "idle"),
      entry("b", "Bravo", "idle"),
    ]);
    expect(ordered.map((e) => e.agent.name)).toEqual(["Alpha", "Bravo", "Charlie"]);
  });

  it("does not mutate the input array", () => {
    const input = [entry("b", "Bravo", "idle"), entry("a", "Alpha", "attention")];
    const before = input.map((e) => e.agent.id);
    orderRoster(input);
    expect(input.map((e) => e.agent.id)).toEqual(before);
  });
});

describe("AgentRoster rendering", () => {
  it("renders rows in attention order with the CEO pinned", () => {
    renderRoster([
      entry("i", "Idle One", "idle"),
      entry("f", "Friday", "idle", true),
      entry("e", "Errored One", "attention"),
    ]);
    expect(rowNames()).toEqual(["Friday", "Errored One", "Idle One"]);
  });

  it("marks each row with its state so the rail and dot can key off it", () => {
    renderRoster([
      entry("r", "Runner", "running"),
      entry("p", "Sleeper", "paused"),
      entry("i", "Quiet", "idle"),
    ]);
    const state = (name: string) =>
      screen.getByText(name).closest(".agent-item")?.getAttribute("data-state");
    expect(state("Runner")).toBe("running");
    expect(state("Sleeper")).toBe("paused");
    expect(state("Quiet")).toBe("idle");
  });

  it("says what a busy agent is doing instead of which model it runs", () => {
    renderRoster([entry("r", "Runner", "running", false, "anthropic/claude-opus-4-6")]);
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByTestId("live-run-dot")).toBeInTheDocument();
    expect(screen.queryByText("anthropic/claude-opus-4-6")).not.toBeInTheDocument();
  });

  it("falls back to the model line only when the agent has nothing to report", () => {
    renderRoster([entry("i", "Quiet", "idle", false, "anthropic/claude-opus-4-6")]);
    expect(screen.getByText("anthropic/claude-opus-4-6")).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    // No marker at all on a calm row — absence IS the idle state.
    expect(screen.queryByTestId("live-run-dot")).not.toBeInTheDocument();
    expect(document.querySelector(".agent-state")).toBeNull();
  });

  it("names the paused and needs-you states in words, not colour alone", () => {
    renderRoster([entry("p", "Sleeper", "paused"), entry("e", "Broken", "attention")]);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Needs you")).toBeInTheDocument();
  });

  it("leads the section label with what needs the human, ahead of live work", () => {
    renderRoster([entry("r", "Runner", "running"), entry("e", "Broken", "attention")]);
    expect(screen.getByText(/1 needs you/)).toBeInTheDocument();
    expect(screen.queryByText(/working/)).not.toBeInTheDocument();
  });

  it("agrees the verb with the count", () => {
    renderRoster([entry("e", "Broken", "attention"), entry("f", "AlsoBroken", "attention")]);
    expect(screen.getByText(/2 need you/)).toBeInTheDocument();
  });

  it("reports the working count when nothing is broken", () => {
    renderRoster([entry("r", "Runner", "running"), entry("s", "Second", "running"), entry("i", "Quiet", "idle")]);
    expect(screen.getByText("2 working")).toBeInTheDocument();
  });

  it("falls back to a plain count when there is no news", () => {
    renderRoster([entry("a", "Ada", "idle"), entry("b", "Bo", "idle")]);
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("marks the CEO row so it can be pinned and ruled off", () => {
    renderRoster([entry("f", "Friday", "idle", true), entry("a", "Ada", "idle")]);
    const friday = screen.getByText("Friday").closest(".agent-item");
    expect(friday?.getAttribute("data-ceo")).toBe("true");
    expect(within(friday as HTMLElement).getByText("CEO")).toBeInTheDocument();
    expect(screen.getByText("Ada").closest(".agent-item")?.getAttribute("data-ceo")).toBeNull();
  });

  it("selects via a real button, so Enter and Space work without a key handler", () => {
    const { onSelect } = renderRoster([entry("a", "Ada", "idle")]);
    const row = screen.getByRole("button", { name: "Ada" });
    expect(row.tagName).toBe("BUTTON");
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("announces the agent's state as part of the row's accessible name", () => {
    // The state line is inside the button, so a screen reader hears "Runner,
    // Working" without needing a separate aria-label to maintain.
    renderRoster([entry("r", "Runner", "running")]);
    expect(screen.getByRole("button", { name: "Runner Working" })).toBeInTheDocument();
  });

  it("marks the active row with aria-current", () => {
    renderRoster([entry("a", "Ada", "idle"), entry("b", "Bo", "idle")], { activeAgentId: "b" });
    expect(screen.getByText("Bo").closest(".agent-item")?.className).toContain("active");
    expect(
      screen.getByText("Bo").closest(".agent-item")?.querySelector(".agent-item__main"),
    ).toHaveAttribute("aria-current", "true");
    expect(
      screen.getByText("Ada").closest(".agent-item")?.querySelector(".agent-item__main"),
    ).not.toHaveAttribute("aria-current");
  });

  it("routes the per-row actions to their handlers", () => {
    const { onOpenWork, onInspect, onRename, onDelete } = renderRoster([entry("a", "Ada", "idle")]);
    fireEvent.click(screen.getByLabelText("See Ada's work"));
    expect(onOpenWork).toHaveBeenCalledWith("a");
    fireEvent.click(screen.getByLabelText("Inspect Ada"));
    expect(onInspect).toHaveBeenCalledWith("a");
    fireEvent.click(screen.getByLabelText("Rename Ada"));
    expect(onRename.mock.calls[0][0].id).toBe("a");
    fireEvent.click(screen.getByLabelText("Delete Ada"));
    expect(onDelete.mock.calls[0][0].id).toBe("a");
  });

  it("hides delete on the default agent, which cannot be removed", () => {
    renderRoster([entry("f", "Friday", "idle", true), entry("a", "Ada", "idle")], {
      defaultAgentId: "f",
    });
    expect(screen.queryByLabelText("Delete Friday")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Delete Ada")).toBeInTheDocument();
  });

  it("nests the session list under the active row only", () => {
    renderRoster([entry("a", "Ada", "idle"), entry("b", "Bo", "idle")], {
      activeAgentId: "a",
      renderSessions: (id: string) => <div data-testid={`sessions-${id}`} />,
    });
    expect(screen.getByTestId("sessions-a")).toBeInTheDocument();
    expect(screen.queryByTestId("sessions-b")).not.toBeInTheDocument();
  });
});
