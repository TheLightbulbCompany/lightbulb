import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

import { OnboardingBand } from "../OnboardingBand";
import { STARTER_AGENT_DEFAULT_NAMES } from "@lightbulb/shared";
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
  membersCount: 1,
  connectionsError: undefined as unknown,
  billing: null as Record<string, unknown> | null,
  navigateShell: vi.fn(),
  contextImported: true as boolean,
  contextError: undefined as unknown,
  setContext: vi.fn(),
  openInviteDialog: vi.fn(),
  routerPush: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: h.routerPush }) }));

// The context card's done-state is the band's one SWR read; mocked like its
// other reads so every render stays synchronous.
vi.mock("swr", () => ({
  default: () => ({ data: { imported: h.contextImported }, error: h.contextError, isLoading: false, mutate: h.setContext }),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: h.billing, isLoading: false }),
}));

vi.mock("@/lib/shellNav", () => ({
  navigateShell: (...args: unknown[]) => h.navigateShell(...args),
}));

vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({
    connections: h.connections, startOAuth: h.startOAuth, isLoading: h.connectionsLoading, error: h.connectionsError,
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ orgId: h.orgId }),
  useOrganization: () => ({
    organization: { id: h.orgId, membersCount: h.membersCount },
    membership: { role: h.role },
  }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: h.get, post: h.post, patch: h.patch }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";

vi.mock("@/components/workspace/workspaceController", () => ({
  openPanel: (...args: unknown[]) => h.openPanel(...args),
  openInviteDialog: () => h.openInviteDialog(),
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
const row = (provider: string, status: Connection["status"]): Connection => ({
  provider, display_name: provider, acquisition: "direct_oauth", status,
});
const connected = (provider: string) => row(provider, "connected");
const CATALOG = ["stripe", "github", "notion"].map((p) => row(p, "disconnected"));
const clipboard = vi.fn();

beforeEach(() => {
  h.connections = CATALOG;
  h.membersCount = 1;
  h.connectionsError = undefined;
  clipboard.mockReset();
  clipboard.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText: clipboard }, configurable: true });
  h.role = "org:admin";
  h.startOAuth.mockReset();
  h.startOAuth.mockResolvedValue(undefined);
  h.get.mockReset();
  h.post.mockReset();
  h.agents = [];
  h.openPanel.mockReset();
  h.patch.mockReset();
  h.patch.mockResolvedValue({});
  h.connectionsLoading = false;
  h.rosterLoading = false;
  h.orgId = "org_a";
  h.billing = null;
  h.navigateShell.mockReset();
  h.contextImported = true;
  h.contextError = undefined;
  h.setContext.mockReset();
  h.openInviteDialog.mockReset();
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
    expect(screen.getByText("Invite your team")).toBeInTheDocument();
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

  it("says All set for one session once every counted step is done, then renders nothing", () => {
    h.connections = ["stripe", "github", "notion"].map(connected);
    h.membersCount = 2;
    const { unmount } = render(<OnboardingBand participants={FRESH} humanSpoke />);
    expect(screen.getByText("All set. Your team can see everything your AI employees do.")).toBeInTheDocument();
    unmount();
    const { container } = render(<OnboardingBand participants={FRESH} humanSpoke />);
    expect(container).toBeEmptyDOMElement();
  });

  it("opens Settings at Connections from the See more card", async () => {
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /See more connections/ }));
    expect(h.routerPush).toHaveBeenCalledWith("/settings?panel=connections");
  });

  it("never counts See more toward completion — it is a link, not a task", () => {
    // All five real steps done. The See more card is still on screen, and the
    // band must still retire; counting it would keep the band up forever.
    h.connections = ["stripe", "github", "notion"].map(connected);
    h.membersCount = 2;
    render(<OnboardingBand participants={FRESH} humanSpoke />);
    expect(screen.getByText(/All set/)).toBeInTheDocument();
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

  it("cannot action the admin-only cards, but can invite (D5)", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send invite" })).toBeInTheDocument();
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
    h.membersCount = 2;
    render(<OnboardingBand participants={FRESH} />);
    const invite = screen.getByRole("button", { name: /Invite your team/ });
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
    h.membersCount = 2;
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Invite your team/ })).toBeEnabled();
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

describe("connector cards", () => {
  it("hides a connector the catalog marks unavailable", () => {
    h.connections = [row("stripe", "unavailable"), ...CATALOG.slice(1)];
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.queryByRole("button", { name: /Stripe/ })).toBeNull();
    expect(screen.getByRole("button", { name: /GitHub/ })).toBeInTheDocument();
  });

  it("renders nothing when the catalog failed to load, rather than calling everything undone", () => {
    h.connectionsError = new Error("boom");
    const { container } = render(<OnboardingBand participants={FRESH} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is busy and inert while the connect call is in flight", async () => {
    h.startOAuth.mockReturnValue(new Promise(() => {}));
    render(<OnboardingBand participants={FRESH} />);
    fireEvent.click(screen.getByRole("button", { name: /Stripe/ }));
    const card = await screen.findByRole("button", { name: /Stripe/ });
    await waitFor(() => expect(card).toBeDisabled());
    expect(card).toHaveTextContent(/Connecting/);
  });

  it("says so on the card when the connect call fails", async () => {
    h.startOAuth.mockRejectedValue(new Error("403"));
    render(<OnboardingBand participants={FRESH} />);
    fireEvent.click(screen.getByRole("button", { name: /Stripe/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Stripe/ })).toHaveTextContent(/Couldn’t connect/));
    expect(screen.getByRole("button", { name: /Stripe/ })).toBeEnabled();
  });
});

describe("08 §3 order", () => {
  it("counts the workspace and its AI employees as done and puts the invite form first", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("heading", { name: "Get started · 2 of 5 done" })).toBeInTheDocument();
    expect(screen.getByText("Your workspace")).toBeInTheDocument();
    expect(screen.getByText("Six of them, in #general")).toBeInTheDocument();
    expect(screen.getByText("Invite your team")).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    // In the band the invite stays until someone joins: no "Just me for now".
    expect(screen.queryByRole("button", { name: "Just me for now" })).toBeNull();
  });

  it("marks the CEO step done once a person has written in the channel", () => {
    render(<OnboardingBand participants={FRESH} humanSpoke />);
    expect(screen.getByRole("button", { name: /Tell Lightbulb about the business/ })).toHaveTextContent(/Lightbulb has it/);
    expect(screen.getByRole("heading", { name: "Get started · 3 of 5 done" })).toBeInTheDocument();
  });

  it("opens the Invite people dialog from a done invite card", async () => {
    h.membersCount = 2;
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Invite your team/ }));
    expect(h.openInviteDialog).toHaveBeenCalled();
  });
});

describe("starter-model card", () => {
  const PAID_NO_PROVIDER = { is_subscribed: true, subscription_status: "active", plan: "standard", provider_choice: null };

  it("renders first in the band for a paid workspace on the starter model", () => {
    h.billing = PAID_NO_PROVIDER;
    render(<OnboardingBand participants={FRESH} />);
    const cards = screen.getAllByRole("button").filter((b) => b.classList.contains("onboarding-card"));
    expect(cards[0]).toHaveTextContent("You're on the starter model");
    expect(cards[0]).toHaveTextContent("Choose OpenRouter managed or sign in with ChatGPT");
  });

  it("opens the provider picker on click", async () => {
    h.billing = PAID_NO_PROVIDER;
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /starter model/ }));
    expect(h.navigateShell).toHaveBeenCalledWith("/settings?panel=billing");
  });

  it("is inert for a plain member, since only an admin can change the provider", () => {
    h.billing = PAID_NO_PROVIDER;
    h.role = "org:member";
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /starter model/ })).toBeDisabled();
  });

  it("is absent once a provider is chosen", () => {
    h.billing = { ...PAID_NO_PROVIDER, provider_choice: "chatgpt_oauth" };
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.queryByRole("button", { name: /starter model/ })).toBeNull();
  });
});

describe("share your context", () => {
  beforeEach(() => { h.contextImported = false; });

  it("sits in the action row beside invite and naming", () => {
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByRole("button", { name: /Share your context/ })).toHaveTextContent(/Paste what you've told Claude or ChatGPT/);
  });

  it("saves the paste to the CEO and says so", async () => {
    h.post.mockResolvedValue({ merged: 20, overflowed: false });
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Share your context/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your context" }), "We sell bread in Lyon");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(h.post).toHaveBeenCalledWith("/company/context", { text: "We sell bread in Lyon" });
    expect(await screen.findByText("Lightbulb has it and will use it from now on.")).toBeInTheDocument();
    // The card flips only once the dialog closes, so the band cannot retire under it.
    expect(h.setContext).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(h.setContext).toHaveBeenCalledWith({ imported: true }, { revalidate: false });
  });

  it("fills the textarea from a markdown file without uploading it", async () => {
    // jsdom has no Blob.text(); every browser the app ships to does.
    if (!File.prototype.text) {
      File.prototype.text = function (this: File) {
        return new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.readAsText(this);
        });
      };
    }
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Share your context/ }));
    const file = new File(["# Us\n- bakery"], "context.md", { type: "text/markdown" });
    await userEvent.upload(screen.getByLabelText("Or load a file"), file);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Your context" })).toHaveValue("# Us\n- bakery"));
    expect(h.post).not.toHaveBeenCalled();
  });

  it("keeps the dialog open with an error when the save fails", async () => {
    h.post.mockRejectedValue(new Error("boom"));
    render(<OnboardingBand participants={FRESH} />);
    await userEvent.click(screen.getByRole("button", { name: /Share your context/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your context" }), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save that");
  });

  it("is done once the CEO holds imported context, and stays open for another paste", () => {
    h.contextImported = true;
    render(<OnboardingBand participants={FRESH} />);
    const card = screen.getByRole("button", { name: /Share your context/ });
    expect(card).toHaveTextContent(/Lightbulb has it/);
    expect(card).toBeEnabled();
  });

  it("is inert for a plain member, who is told to ask an admin", () => {
    h.role = "org:member";
    render(<OnboardingBand participants={FRESH} />);
    const card = screen.getByRole("button", { name: /Share your context/ });
    expect(card).toBeDisabled();
    expect(card).toHaveTextContent(/Ask an admin/);
  });

  it("drops the card when the status read fails, rather than guessing", () => {
    h.contextError = new Error("409");
    render(<OnboardingBand participants={FRESH} />);
    expect(screen.getByText("Invite your team")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Share your context/ })).toBeNull();
  });
});
