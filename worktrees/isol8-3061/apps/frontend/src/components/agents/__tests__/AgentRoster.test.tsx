// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
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

import {
  AgentRoster,
  collapsedRoster,
  orderRoster,
  rosterStateFor,
  type RosterEntry,
} from "../AgentRoster";
import type { Agent } from "@/hooks/useAgents";

const PREFS_KEY = "lightbulb:roster:prefs";

function seedPrefs(prefs: { pinned?: string[]; expanded?: boolean }): void {
  localStorage.setItem(
    PREFS_KEY,
    JSON.stringify({ pinned: prefs.pinned ?? [], expanded: prefs.expanded ?? false }),
  );
}

beforeEach(() => {
  localStorage.clear();
  // The roster now collapses to Friday + pins by default. These suites were
  // written against the always-full roster and assert on every row, so they
  // run expanded; the collapse behaviour has its own describe below.
  seedPrefs({ expanded: true });
});

const agent = (id: string, name: string, model?: string): Agent => ({ id, name, model });

function entry(
  id: string,
  name: string,
  state: RosterEntry["state"],
  isCeo = false,
  model?: string,
  extra: Partial<RosterEntry> = {},
): RosterEntry {
  return { agent: agent(id, name, model), state, isCeo, ...extra };
}

function renderRoster(entries: RosterEntry[], props: Partial<Parameters<typeof AgentRoster>[0]> = {}) {
  const handlers = {
    onSelect: vi.fn(),
    onOpenWork: vi.fn(),
    onInspect: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  };
  const { unmount } = render(
    <AgentRoster
      entries={entries}
      activeAgentId={null}
      publishedAgentIds={new Set<string>()}
      {...handlers}
      {...props}
    />,
  );
  return { ...handlers, unmount };
}

/** Like `renderRoster` but hands back a `rerender` that swaps only `entries`,
 *  so a test can simulate a background status change mid-interaction. */
function renderRosterRerenderable(entries: RosterEntry[]) {
  const props = {
    activeAgentId: null,
    publishedAgentIds: new Set<string>(),
    onSelect: vi.fn(),
    onOpenWork: vi.fn(),
    onInspect: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  };
  const view = render(<AgentRoster entries={entries} {...props} />);
  return {
    rerender: (next: RosterEntry[]) => view.rerender(<AgentRoster entries={next} {...props} />),
  };
}

const rowNames = () =>
  Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map((n) => n.textContent);

describe("rosterStateFor", () => {
  it("shows EVERY live-meaning status as running, not just 'running'", () => {
    // Regression: only `running` was recognised, so an agent reporting
    // `active` / `online` / `thinking` rendered with no rail and sorted as if
    // asleep — the same lie as the hardcoded green dot, inverted.
    for (const status of ["running", "active", "online", "thinking", "covered", "stalled"]) {
      expect(rosterStateFor(status)).toBe("running");
    }
  });

  it("treats 'failed' as needing a human, not as idle", () => {
    expect(rosterStateFor("error")).toBe("attention");
    expect(rosterStateFor("failed")).toBe("attention");
  });

  it("keeps terminated distinct from paused", () => {
    // Folding them together contradicted the header's pause/resume control,
    // which reads `status === "paused"` off the same field.
    expect(rosterStateFor("paused")).toBe("paused");
    expect(rosterStateFor("terminated")).toBe("terminated");
  });

  it("treats an absent status as idle but an unrecognised one as unknown", () => {
    // Absent = the roster read hasn't landed; that really is "nothing to
    // report". Present-but-unrecognised is information we can't classify, and
    // claiming either "idle" or "working" for it would be a guess.
    expect(rosterStateFor(null)).toBe("idle");
    expect(rosterStateFor(undefined)).toBe("idle");
    expect(rosterStateFor("")).toBe("idle");
    expect(rosterStateFor("compacting")).toBe("unknown");
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

  it("sinks terminated agents below paused ones", () => {
    // A paused agent is a click from working again; a terminated one can't be
    // worked with at all, so it should never sit between two that can.
    const ordered = orderRoster([
      entry("t", "Dead One", "terminated"),
      entry("p", "Paused One", "paused"),
      entry("i", "Idle One", "idle"),
    ]);
    expect(ordered.map((e) => e.agent.id)).toEqual(["i", "p", "t"]);
  });

  it("does not move a row for a status it cannot classify", () => {
    const ordered = orderRoster([
      entry("u", "Unknown One", "unknown"),
      entry("a", "Ada", "idle"),
    ]);
    expect(ordered.map((e) => e.agent.id)).toEqual(["a", "u"]);
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

  it("prefers the roster row's own model name, so the sidebar needs no catalog", () => {
    // Ruling M6-R53: on AgentCore `agents.list` carries `modelName`, so the shell never dials
    // `models.list` (a ~55s microVM cold start on BYO/OAuth) to label a row. `configSnapshot`
    // is deliberately absent here — the label must still render.
    const row = entry("i", "Quiet", "idle", false, "arn:aws:bedrock:us-east-1:1:x/opq");
    row.agent = { ...row.agent, modelName: "Claude Opus" };
    renderRoster([row]);
    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
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
    renderRoster([entry("r", "Runner", "running")]);
    expect(screen.getByRole("button", { name: "Runner, Working" })).toBeInTheDocument();
  });

  it("keeps the marketplace pip out of the row's accessible name", () => {
    // The pip lives inside the button, and a button names itself from its
    // content — including a descendant's aria-label. Left alone it prefixed
    // every published agent's row with "Published to marketplace".
    renderRoster([entry("f", "Friday", "running", true)], {
      publishedAgentIds: new Set(["f"]),
    });
    expect(
      screen.getByRole("button", { name: "Friday, CEO, Working, Published to marketplace" }),
    ).toBeInTheDocument();
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

  it("labels terminated and paused differently in one view", () => {
    renderRoster([entry("p", "Sleeper", "paused"), entry("t", "Dead", "terminated")]);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Terminated")).toBeInTheDocument();
    expect(screen.getByText("Dead").closest(".agent-item")?.getAttribute("data-state")).toBe(
      "terminated",
    );
  });

  it("prints an unrecognised status verbatim rather than guessing at it", () => {
    renderRoster([entry("u", "Odd", "unknown", false, undefined, { rawStatus: "compacting" })]);
    expect(screen.getByText("Compacting")).toBeInTheDocument();
    // Not claimed as either idle or working.
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.getByText("Odd").closest(".agent-item")?.getAttribute("data-state")).toBe(
      "unknown",
    );
  });

  it("offers delete for a starter specialist regardless of provenance", () => {
    renderRoster([
      entry("f", "Renamed specialist", "idle", false, undefined, { isSeeded: true }),
      entry("a", "Ada", "idle"),
    ]);
    expect(screen.getByLabelText("Delete Renamed specialist")).toBeInTheDocument();
    expect(screen.getByLabelText("Delete Ada")).toBeInTheDocument();
  });

  it("lets the backend decide whether the current default can be removed", () => {
    renderRoster([entry("f", "Renamed CEO", "idle", true), entry("a", "Ada", "idle")], {
      defaultAgentId: "f",
    });
    expect(screen.getByLabelText("Delete Renamed CEO")).toBeInTheDocument();
    expect(screen.getByLabelText("Delete Ada")).toBeInTheDocument();
  });

  it("freezes the order while the pointer is over the roster", () => {
    // A row must not slide out from under a click that is already on its way
    // because some OTHER agent self-healed. Same principle that keeps a
    // running agent from being hoisted, applied to the states that do move.
    const { rerender } = renderRosterRerenderable([
      entry("e", "Broken", "attention"),
      entry("a", "Ada", "idle"),
    ]);
    expect(rowNames()).toEqual(["Broken", "Ada"]);

    fireEvent.pointerEnter(document.querySelector(".agent-list") as Element);
    // Broken recovers — but the pointer is still on the roster, so nothing moves.
    rerender([entry("e", "Broken", "idle"), entry("a", "Ada", "idle")]);
    expect(rowNames()).toEqual(["Broken", "Ada"]);

    // Pointer leaves: the order re-settles (now alphabetical, both idle).
    fireEvent.pointerLeave(document.querySelector(".agent-list") as Element);
    expect(rowNames()).toEqual(["Ada", "Broken"]);
  });

  it("keeps the freeze while focus moves between a row and its actions", () => {
    const { rerender } = renderRosterRerenderable([
      entry("e", "Broken", "attention"),
      entry("a", "Ada", "idle"),
    ]);
    fireEvent.focus(screen.getByRole("button", { name: "Broken, Needs you" }));
    rerender([entry("e", "Broken", "idle"), entry("a", "Ada", "idle")]);
    expect(rowNames()).toEqual(["Broken", "Ada"]);

    // Tabbing from the row into its own Rename button must NOT thaw.
    fireEvent.blur(screen.getByRole("button", { name: "Broken" }), {
      relatedTarget: screen.getByLabelText("Rename Broken"),
    });
    expect(rowNames()).toEqual(["Broken", "Ada"]);
  });
});

describe("collapsedRoster", () => {
  const ceo = entry("f", "Friday", "idle", true);
  const a = entry("a", "Atlas", "idle");
  const b = entry("b", "Bee", "idle");

  it("keeps the CEO and the pins, drops the rest", () => {
    const kept = collapsedRoster([ceo, a, b], new Set(["b"]));
    expect(kept.map((e) => e.agent.id)).toEqual(["f", "b"]);
  });

  it("keeps the ACTIVE agent, even when it is neither CEO nor pinned", () => {
    // activeAgentId is driven from outside the roster — a run notification, an
    // assignee picker, openChatPanel. Filtering it out would leave the chat
    // pane showing one agent while the sidebar showed only Friday.
    const kept = collapsedRoster([ceo, a, b], new Set(), "b");
    expect(kept.map((e) => e.agent.id)).toEqual(["f", "b"]);
  });

  it("falls back to the first row when NO entry is the CEO", () => {
    // `defaultId` and CEO-ness are decoupled: on a company with no role:"ceo"
    // agent the backend falls back to index 0, so isCeo can be false for every
    // entry. Without this the collapsed roster would be empty and the owner
    // would have nothing to click.
    const kept = collapsedRoster([a, b], new Set());
    expect(kept.map((e) => e.agent.id)).toEqual(["a"]);
  });
});

describe("orderRoster with pins", () => {
  it("sorts pinned agents above unpinned ones, but still under the CEO", () => {
    const ordered = orderRoster(
      [
        entry("a", "Alpha", "attention"),
        entry("f", "Friday", "idle", true),
        entry("z", "Zeta", "idle"),
      ],
      new Set(["z"]),
    );
    // Zeta is idle and last alphabetically, but the owner pinned it, and an
    // explicit choice outranks a derived attention rank.
    expect(ordered.map((e) => e.agent.id)).toEqual(["f", "z", "a"]);
  });
});

describe("AgentRoster collapse", () => {
  const entries = [
    entry("f", "Friday", "idle", true),
    entry("a", "Atlas", "idle"),
    entry("b", "Bee", "attention"),
  ];

  it("shows only the CEO by default, with the others behind the expander", () => {
    localStorage.clear();
    renderRoster(entries);
    expect(rowNames()).toEqual(["Friday"]);
    expect(screen.getByRole("button", { name: /show 2 more agents/i })).toBeInTheDocument();
  });

  it("still reports that a hidden agent needs attention", () => {
    // The whole risk of collapsing: burying the one row that wanted the owner.
    // The section header carries the count for the FULL roster, not the
    // visible slice, so a hidden "needs you" is still announced.
    localStorage.clear();
    renderRoster(entries);
    expect(rowNames()).toEqual(["Friday"]);
    expect(screen.getByText(/1 needs you/i)).toBeInTheDocument();
  });

  it("reveals the rest when the expander is pressed", () => {
    localStorage.clear();
    renderRoster(entries);
    fireEvent.click(screen.getByRole("button", { name: /show 2 more agents/i }));
    expect(rowNames()).toEqual(["Friday", "Bee", "Atlas"]);
    expect(screen.getByRole("button", { name: /show fewer/i })).toBeInTheDocument();
  });

  it("keeps a pinned agent visible while collapsed", () => {
    localStorage.clear();
    seedPrefs({ pinned: ["a"] });
    renderRoster(entries);
    expect(rowNames()).toEqual(["Friday", "Atlas"]);
    expect(screen.getByRole("button", { name: /show 1 more agent$/i })).toBeInTheDocument();
  });

  it("pinning from a row persists and survives a remount", () => {
    localStorage.clear();
    seedPrefs({ expanded: true });
    const { unmount } = renderRoster(entries);
    fireEvent.click(screen.getByRole("button", { name: /pin Atlas to the collapsed roster/i }));
    unmount();

    // Re-render collapsed: the pin the owner just set is what keeps Atlas up.
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...JSON.parse(localStorage.getItem(PREFS_KEY) as string), expanded: false }),
    );
    renderRoster(entries);
    expect(rowNames()).toEqual(["Friday", "Atlas"]);
  });

  it("offers no pin control on the CEO, which is always visible anyway", () => {
    localStorage.clear();
    seedPrefs({ expanded: true });
    renderRoster(entries);
    expect(screen.queryByRole("button", { name: /pin Friday/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pin Atlas/i })).toBeInTheDocument();
  });

  it("shows the open agent in the collapsed roster, with its aria-current row", () => {
    localStorage.clear();
    // Bee needs the owner and is neither CEO nor pinned — exactly the agent a
    // notification toast would open.
    renderRoster(entries, { activeAgentId: "b" });
    expect(rowNames()).toEqual(["Friday", "Bee"]);
    expect(
      screen.getByRole("button", { name: /Bee.*Needs you/i }),
    ).toHaveAttribute("aria-current", "true");
    // Only Atlas is left behind the chevron.
    expect(screen.getByRole("button", { name: /show 1 more agent$/i })).toBeInTheDocument();
  });

  it("renders no expander when there is nothing behind it", () => {
    localStorage.clear();
    renderRoster([entry("f", "Friday", "idle", true)]);
    expect(screen.queryByRole("button", { name: /show .* more/i })).not.toBeInTheDocument();
  });

  it("renders no expander when every agent is pinned, in either state", () => {
    // Nothing is hidden, so the control would collapse to what is already on
    // screen. True whether or not the owner previously expanded.
    for (const expanded of [false, true]) {
      localStorage.clear();
      seedPrefs({ pinned: ["a", "b"], expanded });
      const { unmount } = renderRoster(entries);
      // Both are pinned, so the pin tiebreak is even and attention rank
      // decides: Bee needs the owner, Atlas is idle.
      expect(rowNames()).toEqual(["Friday", "Bee", "Atlas"]);
      expect(screen.queryByRole("button", { name: /show fewer|show .* more/i })).not.toBeInTheDocument();
      unmount();
    }
  });
});
