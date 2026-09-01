// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { PresetCatalog } from "../PresetCatalog";
import { ROUTINE_PRESETS } from "../presets";

const post = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));
const mockConnections = vi.fn();
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => mockConnections() }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn(), notifySuccess: vi.fn() }));

const AGENTS = [
  { id: "agent_atlas", name: "Atlas", role: "general", status: "idle" },
  { id: "agent_friday", name: "Friday", role: "ceo", status: "idle" },
];

beforeEach(() => {
  post.mockReset();
  mockConnections.mockReturnValue({ connections: [] });
});

describe("PresetCatalog", () => {
  it("creates a preset through the builder's exact two calls, assigned to the CEO", async () => {
    // The whole point: a preset is NOT a new kind of thing. It is the same
    // POST /routines + POST /routines/{id}/triggers the schedule rung makes,
    // so what lands is an ordinary Paperclip routine the builder can edit.
    post.mockResolvedValueOnce({ id: "rt_1" }).mockResolvedValueOnce({});
    const onCreated = vi.fn();
    render(<PresetCatalog agents={AGENTS} routineTitles={new Set()} onCreated={onCreated} />);

    fireEvent.click(screen.getByRole("switch", { name: /Switch on Check the company inbox/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(post).toHaveBeenNthCalledWith(
      1,
      "/routines",
      expect.objectContaining({
        title: "Check the company inbox",
        assignee_agent_id: "agent_friday",
        status: "active",
      }),
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/routines/rt_1/triggers",
      expect.objectContaining({ kind: "schedule", cron_expression: "0 8 * * 1-5" }),
    );
  });

  it("disables a preset whose connection is missing and names the fix", () => {
    // "Needs setup" alone says something is wrong without saying what. The
    // provider is named and linked, so it is a next step, not a dead switch.
    render(<PresetCatalog agents={AGENTS} routineTitles={new Set()} onCreated={vi.fn()} />);
    expect(screen.getByRole("switch", { name: /Switch on Post to Instagram/i })).toBeDisabled();
    expect(screen.getByRole("link", { name: /Needs instagram/i })).toHaveAttribute(
      "href",
      "/chat?panel=integrations",
    );
    // ...and one with no requirements is enabled.
    expect(screen.getByRole("switch", { name: /Switch on Check the company inbox/i })).toBeEnabled();
  });

  it("enables a preset once its connection is present", () => {
    mockConnections.mockReturnValue({ connections: [{ provider: "instagram", status: "connected" }] });
    render(<PresetCatalog agents={AGENTS} routineTitles={new Set()} onCreated={vi.fn()} />);
    expect(screen.getByRole("switch", { name: /Switch on Post to Instagram/i })).toBeEnabled();
    expect(screen.queryByRole("link", { name: /Needs instagram/i })).not.toBeInTheDocument();
  });

  it("hides presets already switched on, and renders nothing once all are", () => {
    // Once created, the routine lives in the table with full controls; a
    // second toggle for the same thing would be a worse copy of that.
    const all = new Set(ROUTINE_PRESETS.map((p) => p.title));
    const { container } = render(
      <PresetCatalog agents={AGENTS} routineTitles={all} onCreated={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("every preset that names a requirement names a real connection-rail provider", () => {
    // A typo here ("instgram") would render as permanently "Needs setup"
    // with a link to a row that does not exist.
    const known = new Set(["stripe", "instagram", "x", "posthog", "attio", "apollo", "google", "github"]);
    for (const p of ROUTINE_PRESETS) for (const r of p.requires) expect(known).toContain(r);
  });
});
