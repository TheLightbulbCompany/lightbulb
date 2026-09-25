import { useEffect } from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { MarketplaceToast } from "../MarketplaceToast";
import { MarketplaceDrawerProvider, useMarketplaceDrawer } from "../MarketplaceDrawerProvider";

vi.mock("@clerk/nextjs", () => ({}));

/** Drives the toast by calling showInstallToast on mount, then rendering the
 * real MarketplaceToast against the shared drawer state. */
function ToastHarness({ automationsPending }: { automationsPending?: number }) {
  const { showInstallToast } = useMarketplaceDrawer();
  useEffect(() => {
    showInstallToast({ agentName: "Atlas", agentId: "agent_atlas", automationsPending });
  }, [showInstallToast, automationsPending]);
  return <MarketplaceToast />;
}

function renderToast(automationsPending?: number) {
  return render(
    <MarketplaceDrawerProvider>
      <ToastHarness automationsPending={automationsPending} />
    </MarketplaceDrawerProvider>,
  );
}

describe("MarketplaceToast — pending automations", () => {
  it("tells the buyer how many automations still need wiring", () => {
    renderToast(2);
    expect(screen.getByTestId("toast-automations-pending")).toHaveTextContent(
      /2 automations need wiring/,
    );
  });

  it("reads naturally for a single automation", () => {
    renderToast(1);
    expect(screen.getByTestId("toast-automations-pending")).toHaveTextContent(
      /1 automation needs wiring/,
    );
  });

  it("says nothing when the listing shipped no webhook automations", () => {
    renderToast();
    expect(screen.queryByTestId("toast-automations-pending")).toBeNull();
  });

  it("says nothing for a zero count", () => {
    renderToast(0);
    expect(screen.queryByTestId("toast-automations-pending")).toBeNull();
  });
});
