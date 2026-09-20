import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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
}));

vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections: h.connections, startOAuth: h.startOAuth, isLoading: false }),
}));

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({
    organization: { id: "org_a" },
    membership: { role: h.role },
  }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: h.get, post: h.post, patch: vi.fn() }),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: h.agents }, mutate: vi.fn() }),
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
  window.localStorage.clear();
});

describe("OnboardingBand", () => {
  it("renders a card for each outstanding step on a fresh workspace", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /GitHub/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Notion/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /PostHog/ })).toBeInTheDocument();
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
    h.connections = ["stripe", "github", "notion", "posthog"].map(connected);
    const { container } = render(<OnboardingBand participants={[
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      participant("agent", "agent_0", "Ada"),
    ]} />);
    expect(container).toBeEmptyDOMElement();
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
