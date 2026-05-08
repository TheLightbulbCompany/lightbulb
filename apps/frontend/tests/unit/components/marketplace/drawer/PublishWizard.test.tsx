import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/components/marketplace/drawer/wizardSteps", () => ({
  PickAgentStep: ({ onSelect, selected }: { onSelect: (id: string) => void; selected?: string }) => (
    <div>
      <span>step1</span>
      <button onClick={() => onSelect("agent_pulse")}>pick</button>
      {selected && <span>selected={selected}</span>}
    </div>
  ),
  DetailsStep: ({ form }: { form: { name: string; tagline: string } }) => (
    <div>step2 — name={form.name} tag={form.tagline}</div>
  ),
  PricingStep: () => <div>step3</div>,
  ReviewStep: () => <div>step4</div>,
  SuccessStep: () => <div>step5</div>,
}));

const setView = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ setView, state: { publishAgentId: undefined } }),
}));

import { PublishWizard } from "@/components/marketplace/drawer/PublishWizard";

describe("PublishWizard", () => {
  it("renders Step 1 by default", () => {
    render(<PublishWizard />);
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("Continue is disabled until Step 1 selects an agent", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
    await user.click(screen.getByText("pick"));
    expect(screen.getByRole("button", { name: /continue/i })).toBeEnabled();
  });

  it("Continue advances Step 1 → 2", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText(/^step2/)).toBeInTheDocument();
  });

  it("Back button goes from Step 2 → 1", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await user.click(screen.getByRole("button", { name: /^back/i }));
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("Cancel returns to prevView", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(setView).toHaveBeenCalledWith("browse");
  });

  it("Step 4 Continue text is 'Submit for review'", async () => {
    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 2
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 3
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 4
    expect(screen.getByRole("button", { name: /submit for review/i })).toBeInTheDocument();
  });
});

const useAgentsMock = vi.fn();
const myAgentsSummaryMock = vi.fn();

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => useAgentsMock(),
  agentDisplayName: (a: { name?: string; identity?: { name?: string } }) =>
    a.identity?.name || a.name || "?",
}));
vi.mock("@/lib/marketplace/api", () => ({
  myAgentsSummary: (opts: unknown) => myAgentsSummaryMock(opts),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_abc") }),
}));

describe("PickAgentStep", () => {
  it("renders agents from useAgents and marks main as disabled", async () => {
    useAgentsMock.mockReturnValue({
      agents: [
        { id: "agent_main", name: "main", identity: { name: "main" } },
        { id: "agent_pulse", name: "pulse", identity: { name: "Pulse" } },
      ],
      defaultId: "agent_main",
    });
    myAgentsSummaryMock.mockResolvedValueOnce({
      agents: [
        { agent_id: "agent_main", name: "main", skill_count: 0, channel_count: 0, cron_count: 0 },
        { agent_id: "agent_pulse", name: "Pulse", skill_count: 3, channel_count: 1, cron_count: 1 },
      ],
    });
    const onSelect = vi.fn();
    const { PickAgentStep } = await vi.importActual<
      typeof import("@/components/marketplace/drawer/wizardSteps")
    >("@/components/marketplace/drawer/wizardSteps");
    render(<PickAgentStep onSelect={onSelect} />);
    await waitFor(() => expect(screen.getByText("Pulse")).toBeInTheDocument());
    expect(screen.getByText(/3 skills/)).toBeInTheDocument();
    expect(screen.getByText(/1 cron/)).toBeInTheDocument();

    const mainCard = screen.getByText("main").closest('[role="button"]');
    expect(mainCard).toHaveAttribute("aria-disabled", "true");
  });

  it("clicking a card calls onSelect with agent id", async () => {
    useAgentsMock.mockReturnValue({
      agents: [{ id: "agent_pulse", name: "pulse", identity: { name: "Pulse" } }],
      defaultId: "agent_main",
    });
    myAgentsSummaryMock.mockResolvedValueOnce({ agents: [] });
    const onSelect = vi.fn();
    const user = userEvent.setup();
    const { PickAgentStep } = await vi.importActual<
      typeof import("@/components/marketplace/drawer/wizardSteps")
    >("@/components/marketplace/drawer/wizardSteps");
    render(<PickAgentStep onSelect={onSelect} />);
    await user.click(screen.getByText("Pulse"));
    expect(onSelect).toHaveBeenCalledWith("agent_pulse");
  });
});
