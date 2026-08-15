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
function ToastHarness({
  acpNote,
  automationsPending,
}: {
  acpNote?: AcpNote;
  automationsPending?: number;
}) {
  const { showInstallToast } = useMarketplaceDrawer();
  useEffect(() => {
    showInstallToast({ agentName: "Atlas", agentId: "agent_atlas", acpNote, automationsPending });
  }, [showInstallToast, acpNote, automationsPending]);
  return <MarketplaceToast />;
}

function renderToast(acpNote?: AcpNote, automationsPending?: number) {
  return render(
    <MarketplaceDrawerProvider>
      <ToastHarness acpNote={acpNote} automationsPending={automationsPending} />
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

describe("MarketplaceToast — pending automations", () => {
  it("tells the buyer how many automations still need wiring", () => {
    renderToast(undefined, 2);
    expect(screen.getByTestId("toast-automations-pending")).toHaveTextContent(
      /2 automations need wiring/,
    );
  });

  it("reads naturally for a single automation", () => {
    renderToast(undefined, 1);
    expect(screen.getByTestId("toast-automations-pending")).toHaveTextContent(
      /1 automation needs wiring/,
    );
  });

  it("says nothing when the listing shipped no webhook automations", () => {
    renderToast(undefined, undefined);
    expect(screen.queryByTestId("toast-automations-pending")).toBeNull();
  });

  it("says nothing for a zero count", () => {
    renderToast(undefined, 0);
    expect(screen.queryByTestId("toast-automations-pending")).toBeNull();
  });
});
