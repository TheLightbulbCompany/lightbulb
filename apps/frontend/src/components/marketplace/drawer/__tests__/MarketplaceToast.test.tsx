import { useEffect } from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { MarketplaceToast } from "../MarketplaceToast";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
  type AcpNote,
} from "../MarketplaceDrawerProvider";

vi.mock("@clerk/nextjs", () => ({}));

/** Drives the toast by calling showInstallToast on mount, then rendering the
 * real MarketplaceToast against the shared drawer state. acpNote is optional
 * so the not-requested case can omit it entirely. */
function ToastHarness({ acpNote }: { acpNote?: AcpNote }) {
  const { showInstallToast } = useMarketplaceDrawer();
  useEffect(() => {
    showInstallToast({ agentName: "Atlas", agentId: "agent_atlas", acpNote });
  }, [showInstallToast, acpNote]);
  return <MarketplaceToast />;
}

function renderToast(acpNote?: AcpNote) {
  return render(
    <MarketplaceDrawerProvider>
      <ToastHarness acpNote={acpNote} />
    </MarketplaceDrawerProvider>,
  );
}

describe("MarketplaceToast — ACP delegation note", () => {
  it("shows the enabled note when ACP was requested and turned on", () => {
    renderToast("enabled");
    expect(screen.getByText(/Atlas installed/)).toBeInTheDocument();
    expect(screen.getByTestId("toast-acp-enabled")).toHaveTextContent(
      /delegate coding tasks to a Claude Code \/ Codex harness/,
    );
    expect(screen.queryByTestId("toast-acp-unavailable")).toBeNull();
  });

  it("shows the softer warning when ACP was requested but unavailable", () => {
    renderToast("unavailable");
    expect(screen.getByTestId("toast-acp-unavailable")).toHaveTextContent(
      /wants a coding harness, but your current provider can't run one yet/,
    );
    expect(screen.queryByTestId("toast-acp-enabled")).toBeNull();
  });

  it("shows no ACP note when ACP was not requested (unchanged install toast)", () => {
    renderToast(undefined);
    expect(screen.getByText(/Atlas installed/)).toBeInTheDocument();
    expect(screen.queryByTestId("toast-acp-enabled")).toBeNull();
    expect(screen.queryByTestId("toast-acp-unavailable")).toBeNull();
  });
});
