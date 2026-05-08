import { describe, expect, it } from "vitest";
import { render, act } from "@testing-library/react";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

// Test-only probe: surface the live drawer-context value so assertions can
// inspect it. Mutating a module-level binding from inside a component would
// trip the react-hooks/globals rule; mutating a property of a stable holder
// object trips react-hooks/immutability. Both rules are OK here — Probe
// renders as a leaf, never re-renders against this value, and the test only
// reads after the hook fires. Disable the immutability rule narrowly.
const capturedRef: { current: ReturnType<typeof useMarketplaceDrawer> | null } = {
  current: null,
};

function Probe() {
  // eslint-disable-next-line react-hooks/immutability
  capturedRef.current = useMarketplaceDrawer();
  return null;
}

function renderWithProvider() {
  return render(
    <MarketplaceDrawerProvider>
      <Probe />
    </MarketplaceDrawerProvider>,
  );
}

describe("MarketplaceDrawerProvider", () => {
  it("starts closed in browse view", () => {
    renderWithProvider();
    expect(capturedRef.current?.state.isOpen).toBe(false);
    expect(capturedRef.current?.state.view).toBe("browse");
  });

  it("open() sets isOpen=true and switches view", () => {
    renderWithProvider();
    act(() => capturedRef.current?.open({ view: "publish" }));
    expect(capturedRef.current?.state.isOpen).toBe(true);
    expect(capturedRef.current?.state.view).toBe("publish");
  });

  it("close() resets transient wizard state", () => {
    renderWithProvider();
    act(() => capturedRef.current?.open({ view: "publish", agentId: "a1" }));
    expect(capturedRef.current?.state.publishAgentId).toBe("a1");
    act(() => capturedRef.current?.close());
    expect(capturedRef.current?.state.isOpen).toBe(false);
    expect(capturedRef.current?.state.publishAgentId).toBeUndefined();
  });

  it("setView remembers prevView when entering publish from mine", () => {
    renderWithProvider();
    act(() => capturedRef.current?.open({ view: "mine" }));
    act(() => capturedRef.current?.setView("publish"));
    expect(capturedRef.current?.state.prevView).toBe("mine");
  });

  it("showInstallToast sets toast state and clears after dismiss", () => {
    renderWithProvider();
    act(() => capturedRef.current?.showInstallToast({ agentName: "Pulse", agentId: "a1" }));
    expect(capturedRef.current?.state.toast).toEqual({ agentName: "Pulse", agentId: "a1" });
    act(() => capturedRef.current?.dismissToast());
    expect(capturedRef.current?.state.toast).toBeNull();
  });

  it("setFreshlyInstalled tracks the agent id", () => {
    renderWithProvider();
    act(() => capturedRef.current?.setFreshlyInstalled("a_new"));
    expect(capturedRef.current?.state.freshlyInstalledAgentId).toBe("a_new");
  });
});
