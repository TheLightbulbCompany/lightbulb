import { describe, expect, it } from "vitest";
import { render, act } from "@testing-library/react";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

let captured: ReturnType<typeof useMarketplaceDrawer> | null = null;

function Probe() {
  captured = useMarketplaceDrawer();
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
    expect(captured?.state.isOpen).toBe(false);
    expect(captured?.state.view).toBe("browse");
  });

  it("open() sets isOpen=true and switches view", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "publish" }));
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("publish");
  });

  it("close() resets transient wizard state", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "publish", agentId: "a1" }));
    expect(captured?.state.publishAgentId).toBe("a1");
    act(() => captured?.close());
    expect(captured?.state.isOpen).toBe(false);
    expect(captured?.state.publishAgentId).toBeUndefined();
  });

  it("setView remembers prevView when entering publish from mine", () => {
    renderWithProvider();
    act(() => captured?.open({ view: "mine" }));
    act(() => captured?.setView("publish"));
    expect(captured?.state.prevView).toBe("mine");
  });

  it("showInstallToast sets toast state and clears after dismiss", () => {
    renderWithProvider();
    act(() => captured?.showInstallToast({ agentName: "Pulse", agentId: "a1" }));
    expect(captured?.state.toast).toEqual({ agentName: "Pulse", agentId: "a1" });
    act(() => captured?.dismissToast());
    expect(captured?.state.toast).toBeNull();
  });

  it("setFreshlyInstalled tracks the agent id", () => {
    renderWithProvider();
    act(() => captured?.setFreshlyInstalled("a_new"));
    expect(captured?.state.freshlyInstalledAgentId).toBe("a_new");
  });
});
