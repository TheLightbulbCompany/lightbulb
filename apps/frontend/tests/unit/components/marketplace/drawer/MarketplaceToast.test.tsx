import { describe, expect, it, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";

vi.useFakeTimers();

const dismissToast = vi.fn();
const stateRef = {
  current: {
    toast: { agentName: "Pulse", agentId: "ag_42" } as { agentName: string; agentId: string } | null,
  },
};

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: stateRef.current,
    dismissToast,
  }),
}));

import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";

describe("MarketplaceToast", () => {
  it("renders toast when state.toast is set", () => {
    stateRef.current = { toast: { agentName: "Pulse", agentId: "ag_42" } };
    render(<MarketplaceToast />);
    expect(screen.getByText(/pulse installed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open in chat/i })).toBeVisible();
  });

  it("auto-dismisses after 5500ms", () => {
    stateRef.current = { toast: { agentName: "Pulse", agentId: "ag_42" } };
    render(<MarketplaceToast />);
    act(() => {
      vi.advanceTimersByTime(5500);
    });
    expect(dismissToast).toHaveBeenCalled();
  });

  it("renders nothing when state.toast is null", () => {
    stateRef.current = { toast: null };
    const { container } = render(<MarketplaceToast />);
    expect(container.firstChild).toBeNull();
  });
});
