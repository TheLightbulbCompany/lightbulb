import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

import { OnboardingBand } from "../OnboardingBand";
import { STARTER_AGENT_DEFAULT_NAMES } from "../onboardingSteps";
import type { Connection } from "@/hooks/useConnections";
import type { RoomParticipant } from "../types";

const h = vi.hoisted(() => ({
  connections: [] as Connection[],
  role: "org:admin" as string,
  startOAuth: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
  agents: [] as { id: string; name: string }[],
  openPanel: vi.fn(),
  connectionsLoading: false,
  rosterLoading: false,
  orgId: "org_a",
  patch: vi.fn(),
}));

vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections: h.connections, startOAuth: h.startOAuth, isLoading: h.connectionsLoading }),
}));

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({
    organization: { id: h.orgId },
    membership: { role: h.role },
  }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: h.get, post: h.post, patch: h.patch }),
}));

vi.mock("@/components/workspace/workspaceController", () => ({
  openPanel: (...args: unknown[]) => h.openPanel(...args),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: h.rosterLoading ? undefined : { agents: h.agents }, isLoading: h.rosterLoading, mutate: vi.fn() }),
  }),
}));

const participant = (kind: "human" | "agent", id: string, displayName: string): RoomParticipant => ({
  kind, id, displayName,
  mentionHandle: kind === "agent" ? displayName.toLowerCase() : null,
  role: "member", listenMode: null,
  joinedAt: "2026-09-20T00:00:00Z", joinedSeq: 0,
});

const STARTER_SIX = [...STARTER_AGENT_DEFAULT_NAMES].map((n, i) => participant("agent", `agent_${i}`, n));
const FRESH: RoomParticipant[] = [participant("human", "user_a", "Aden"), ...STARTER_SIX];
const connected = (provider: string): Connection => ({
  provider, display_name: provider, acquisition: "direct_oauth", status: "connected",
});

beforeEach(() => {
  h.connections = [];
  h.role = "org:admin";
  h.startOAuth.mockReset();
  h.get.mockReset();
  h.post.mockReset();
  h.agents = [];
  h.openPanel.mockReset();
  h.patch.mockReset();
  h.patch.mockResolvedValue({});
  h.connectionsLoading = false;
  h.rosterLoading = false;
  h.orgId = "org_a";
  window.localStorage.clear();
});

describe("OnboardingBand", () => {
  it("renders a card for each outstanding step on a fresh workspace", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /GitHub/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Notion/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /PostHog/ })).toBeNull();
    expect(screen.getByRole("button", { name: /See more connections/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Invite your teammates/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Name your agents/ })).toBeInTheDocument();
  });

  it("starts the provider's OAuth when a connector card is clicked", async () => {
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Stripe/ }));
    expect(h.startOAuth).toHaveBeenCalledWith("stripe");
  });

  it("marks a connected provider as done instead of offering it again", () => {
    h.connections = [connected("stripe")];
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Stripe/ })).toHaveTextContent(/Connected/);
  });

  it("renders nothing at all once every step is done", () => {
    h.connections = ["stripe", "github", "notion"].map(connected);
    const { container } = render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      participant("agent", "agent_0", "Ada"),
    ]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("opens the Connections panel from the See more card", async () => {
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /See more connections/ }));
    expect(h.openPanel).toHaveBeenCalledWith("connections", {}, { pushUrl: true });
  });

  it("never counts See more toward completion — it is a link, not a task", () => {
    // All five real steps done. The See more card is still on screen, and the
    // band must still retire; counting it would keep the band up forever.
    h.connections = ["stripe", "github", "notion"].map(connected);
    const { container } = render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      participant("agent", "agent_0", "Ada"),
    ]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lets a plain member browse connections even though they cannot connect one", () => {
    h.role = "org:member";
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /See more connections/ })).toBeEnabled();
  });

  it("disappears when dismissed", async () => {
    const { container } = render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Dismiss/ }));
    expect(container).toBeEmptyDOMElement();
  });
});

describe("a member who is not an org admin", () => {
  beforeEach(() => { h.role = "org:member"; });

  it("cannot action the admin-only cards", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Invite your teammates/ })).toBeDisabled();
  });

  it("is told why, rather than being handed a 403 on click", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getAllByText(/Ask an admin/).length).toBeGreaterThan(0);
  });

  it("can still name the agents, which the backend does not gate", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Name your agents/ })).toBeEnabled();
  });
});

describe("done-state copy", () => {
  it("does not call an invited teammate a connection", () => {
    render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      ...STARTER_SIX,
    ]} />);
    const invite = screen.getByRole("button", { name: /Invite your teammates/ });
    expect(invite).not.toHaveTextContent(/Connected/);
    expect(invite).toHaveTextContent(/Teammates joined/);
  });

  it("does not call a renamed agent a connection", () => {
    render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("agent", "agent_0", "Ada"),
    ]} />);
    const naming = screen.getByRole("button", { name: /Name your agents/ });
    expect(naming).not.toHaveTextContent(/Connected/);
    expect(naming).toHaveTextContent(/Named/);
  });

  it("still says Connected for an actual connector", () => {
    h.connections = [connected("stripe")];
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toHaveTextContent(/Connected/);
  });
});

describe("a done card", () => {
  it("goes inert for a connector, where reconnecting means nothing", () => {
    h.connections = [connected("stripe")];
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeDisabled();
  });

  it("stays clickable for naming — one renamed agent leaves five to go", () => {
    render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("agent", "agent_0", "Ada"),
    ]} />);
    expect(screen.getByRole("button", { name: /Name your agents/ })).toBeEnabled();
  });

  it("stays clickable for invite, so the link can be copied again", () => {
    render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      ...STARTER_SIX,
    ]} />);
    expect(screen.getByRole("button", { name: /Invite your teammates/ })).toBeEnabled();
  });
});

describe("loading", () => {
  it("renders nothing until the connections catalog has answered", () => {
    // Otherwise a fully set-up workspace sees every card flash as outstanding
    // while /connections is still in flight.
    h.connectionsLoading = true;
    const { container } = render(<OnboardingBand participants={FRESH} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing until the agent roster has answered", () => {
    h.rosterLoading = true;
    const { container } = render(<OnboardingBand participants={FRESH} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("dismissal", () => {
  it("is remembered per workspace, not across all of them", async () => {
    const { unmount } = render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Dismiss/ }));
    unmount();

    // A different Clerk org — brand new, nothing set up. It must still ask.
    h.orgId = "org_b";
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeInTheDocument();
  });

  it("stays dismissed for the workspace it was dismissed in", async () => {
    const { unmount } = render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Dismiss/ }));
    unmount();
    const { container } = render(<OnboardingBand participants={FRESH} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("renaming agents", () => {
  const openNaming = async () => {
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Name your agents/ }));
  };

  it("tells the user when a rename did not save, and keeps the panel open", async () => {
    h.patch.mockRejectedValue(new Error("boom"));
    await openNaming();
    const input = screen.getByLabelText("CEO");
    await userEvent.clear(input);
    await userEvent.type(input, "Ada");
    await userEvent.click(screen.getByRole("button", { name: /Save names/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/save/i);
    expect(screen.getByRole("button", { name: /Save names/ })).toBeInTheDocument();
  });

  it("sends nothing when no name was touched", async () => {
    await openNaming();
    await userEvent.click(screen.getByRole("button", { name: /Save names/ }));
    expect(h.patch).not.toHaveBeenCalled();
  });

  it("shows the live roster name in the field, not the stale member row", () => {
    // The member row still says "CEO"; the roster is the truth.
    h.agents = [{ id: "agent_0", name: "Ada" }];
    render(<OnboardingBand participants={FRESH} />);
    fireEvent.click(screen.getByRole("button", { name: /Name your agents/ }));
    expect(screen.getByDisplayValue("Ada")).toBeInTheDocument();
  });
});
