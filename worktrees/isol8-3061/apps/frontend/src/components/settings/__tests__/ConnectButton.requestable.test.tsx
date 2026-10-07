import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

import { ConnectButton } from "../ConnectButton";
import { type Connection } from "@/hooks/useConnections";

// Render next/link as a plain anchor so the test needs no router context
// (mirrors ConnectButton.test.tsx / ConnectButton.byo.test.tsx).
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...props
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

// ConnectButton calls useSupport() directly for the Request access affordance.
// useSupport() throws outside a <SupportProvider>, so the module is mocked.
const openSupport = vi.fn();
vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport, closeSupport: vi.fn() }),
}));

const requestableRow: Connection = {
  provider: "instagram",
  display_name: "Instagram",
  acquisition: "direct_oauth",
  status: "unavailable",
  requestable: true,
};

function renderButton(overrides: Partial<React.ComponentProps<typeof ConnectButton>> = {}) {
  return render(
    <ConnectButton
      connection={requestableRow}
      canManage
      connectPaste={vi.fn()}
      startOAuth={vi.fn()}
      disconnect={vi.fn()}
      {...overrides}
    />,
  );
}

describe("ConnectButton requestable", () => {
  it("offers no action for a provider the current runtime cannot use", () => {
    renderButton({ connection: { ...requestableRow, requestable: false } });

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("offers Request access and no Connect for a gated provider", () => {
    renderButton();

    expect(screen.getByRole("button", { name: /request access/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^connect$/i })).not.toBeInTheDocument();
  });

  it("prefills the support ticket with the provider name", () => {
    renderButton();

    fireEvent.click(screen.getByRole("button", { name: /request access/i }));

    expect(openSupport).toHaveBeenCalledWith({
      category: "Integration request",
      message: "I'd like to use Instagram.",
    });
  });

  it("disables Request access for a non-admin member", () => {
    renderButton({ canManage: false });

    const button = screen.getByRole("button", { name: /request access/i });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(openSupport).not.toHaveBeenCalled();
  });
});
