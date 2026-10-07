import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const api = { get: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));
const billing = {
  account: { is_subscribed: true } as { is_subscribed: boolean } | null,
  isLoading: false,
  error: undefined as Error | undefined,
};
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing }));

import { SlackInstallCard } from "../SlackInstallCard";

describe("SlackInstallCard", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.del.mockReset();
    billing.account = { is_subscribed: true };
    billing.isLoading = false;
    billing.error = undefined;
  });

  it("still offers the button when the billing read itself fails, and relays the backend's 402", async () => {
    billing.account = null;
    billing.error = new Error("boom");
    api.get.mockImplementation(async (path: string) => {
      if (path.startsWith("/slack/install")) return { installs: [] };
      throw Object.assign(new Error("payment required"), { status: 402 });
    });
    render(<SlackInstallCard agentId="agent_f" />);
    fireEvent.click(await screen.findByRole("button", { name: /add to slack/i }));
    expect(await screen.findByText(/needs a paid plan/i)).toBeInTheDocument();
  });

  it("offers no button to a trial owner, only the paid-plan note", async () => {
    billing.account = { is_subscribed: false };
    api.get.mockResolvedValue({ installs: [] });
    render(<SlackInstallCard agentId="agent_f" />);
    expect(await screen.findByText(/needs a paid plan/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add to slack/i })).toBeNull();
  });

  it("shows Add to Slack when this agent has no install and navigates to the authorize url", async () => {
    api.get.mockImplementation(async (path: string) =>
      path.startsWith("/slack/install") ? { installs: [] } : { url: "https://slack.com/oauth/v2/authorize?x=1" },
    );
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, search: "" }, writable: true });
    render(<SlackInstallCard agentId="agent_f" />);
    const btn = await screen.findByRole("button", { name: /add to slack/i });
    fireEvent.click(btn);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://slack.com/oauth/v2/authorize?x=1"));
    expect(api.get).toHaveBeenCalledWith("/slack/oauth/start?agent_id=agent_f");
  });

  it("shows the connected workspace and can disconnect", async () => {
    api.get.mockResolvedValue({
      installs: [{ team_id: "T1", team_name: "Acme", agent_id: "agent_f", status: "active", installed_at: "" }],
    });
    api.del.mockResolvedValue({ status: "revoked" });
    render(<SlackInstallCard agentId="agent_f" />);
    expect(await screen.findByText(/connected to acme/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /disconnect/i }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/slack/install/T1"));
  });

  it("ignores installs bound to other agents", async () => {
    api.get.mockResolvedValue({
      installs: [{ team_id: "T1", team_name: "Acme", agent_id: "agent_other", status: "active", installed_at: "" }],
    });
    render(<SlackInstallCard agentId="agent_f" />);
    expect(await screen.findByRole("button", { name: /add to slack/i })).toBeInTheDocument();
  });

  it("shows an error with retry when the install list fails to load, and retries", async () => {
    api.get.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce({ installs: [] });
    render(<SlackInstallCard agentId="agent_f" />);
    fireEvent.click(await screen.findByRole("button", { name: /retry/i }));
    expect(await screen.findByRole("button", { name: /add to slack/i })).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledTimes(2);
  });
});
