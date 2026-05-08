import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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
