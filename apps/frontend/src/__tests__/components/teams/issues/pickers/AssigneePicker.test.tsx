import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import type { CompanyAgent } from "@/components/teams/shared/types";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill (same pattern as inboxStorage.test.ts).
beforeAll(() => {
  const store = new Map<string, string>();
  const polyfill: Storage = {
    getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: polyfill,
    writable: true,
    configurable: true,
  });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "localStorage", {
      value: polyfill,
      writable: true,
      configurable: true,
    });
  }
  Storage.prototype.getItem = function (k: string) { return polyfill.getItem(k); };
  Storage.prototype.setItem = function (k: string, v: string) { polyfill.setItem(k, v); };
  Storage.prototype.removeItem = function (k: string) { polyfill.removeItem(k); };
  Storage.prototype.clear = function () { polyfill.clear(); };
});

const agents: CompanyAgent[] = [
  { id: "ag_1", name: "Friday" },
  { id: "ag_2", name: "Monday" },
  { id: "ag_3", name: "Saturday" },
];

describe("AssigneePicker", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders the trigger with the current agent's name", () => {
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId="ag_1"
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Friday/ })).toBeInTheDocument();
  });

  it("renders 'Unassigned' on the trigger when no agent is selected", () => {
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId={null}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Unassigned/i })).toBeInTheDocument();
  });

  it("opens the popover and lists agents on trigger click", async () => {
    const user = userEvent.setup();
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId={null}
        onChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    expect(await screen.findByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("Monday")).toBeInTheDocument();
    expect(screen.getByText("Saturday")).toBeInTheDocument();
  });

  it("filters the list by the search query", async () => {
    const user = userEvent.setup();
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId={null}
        onChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    const search = await screen.findByPlaceholderText(/Search agents/i);
    await user.type(search, "Mon");
    expect(screen.getByText("Monday")).toBeInTheDocument();
    expect(screen.queryByText("Friday")).not.toBeInTheDocument();
    expect(screen.queryByText("Saturday")).not.toBeInTheDocument();
  });

  it("calls onChange with the agent id when an option is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId={null}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    await user.click(await screen.findByText("Monday"));
    expect(onChange).toHaveBeenCalledWith("ag_2");
  });

  it("calls onChange(null) when the None option is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId="ag_1"
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Friday/ }));
    await user.click(await screen.findByText(/No assignee/i));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("bubbles recent agents to the top of the list", async () => {
    const user = userEvent.setup();
    // Pre-seed: "Saturday" is the most-recently-picked.
    localStorage.setItem(
      "isol8.teams.recent-assignees",
      JSON.stringify(["ag_3"]),
    );
    render(
      <AssigneePicker
        agents={agents}
        selectedAgentId={null}
        onChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    const options = await screen.findAllByRole("option");
    // Order: None, Saturday (recent), then the rest in original order.
    expect(options[0]).toHaveTextContent(/No assignee/i);
    expect(options[1]).toHaveTextContent("Saturday");
  });
});
