import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

import { ConnectButton } from "../ConnectButton";
import { ApiError, type Connection } from "@/hooks/useConnections";

// The Browser panel is a dock singleton; the button only asks for it.

// Render next/link as a plain anchor so the test needs no router context.
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

const googleRow: Connection = {
  provider: "google",
  display_name: "Google Workspace",
  acquisition: "direct_oauth",
  status: "disconnected",
};

function renderButton(overrides: Partial<React.ComponentProps<typeof ConnectButton>> = {}) {
  return render(
    <ConnectButton
      connection={googleRow}
      canManage
      connectPaste={vi.fn()}
      startOAuth={vi.fn()}
      disconnect={vi.fn()}
      {...overrides}
    />,
  );
}

describe("ConnectButton", () => {
  it("lets an owner reconnect a migrated OAuth grant while preserving Disconnect", async () => {
    const startOAuth = vi.fn().mockResolvedValue(undefined);
    const disconnect = vi.fn();
    renderButton({
      connection: { ...googleRow, provider: "sentry", display_name: "Sentry", status: "reconnect_required" },
      startOAuth,
      disconnect,
    });

    fireEvent.click(screen.getByRole("button", { name: /reconnect sentry/i }));
    await waitFor(() => expect(startOAuth).toHaveBeenCalledWith("sentry"));
    expect(screen.getByRole("button", { name: /disconnect sentry/i })).toBeInTheDocument();
  });

  it("renders Connect for an OAuth provider", () => {
    renderButton();

    expect(screen.getByRole("button", { name: /connect google workspace/i })).toBeInTheDocument();
  });

  it("states the combined format for a two-value paste provider", () => {
    // Shopify packs the store domain and the token into the one input. The
    // format reaches the owner only through token_paste_hint, so a dropped DTO
    // key would leave them with an unanswerable box and a generic 422.
    renderButton({
      connection: {
        provider: "shopify",
        display_name: "Shopify",
        acquisition: "paste_token",
        status: "disconnected",
        token_paste_hint: "mystore.myshopify.com|shpat_…",
      },
    });

    expect(screen.getByPlaceholderText("mystore.myshopify.com|shpat_…")).toBeInTheDocument();
    // Persistent, because the placeholder is gone on the first keystroke.
    expect(screen.getByText("mystore.myshopify.com|shpat_…")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/Shopify credentials, combined as mystore\.myshopify\.com/i),
    ).toBeInTheDocument();
  });

  it("shows the admin-denial copy for a 403", async () => {
    const startOAuth = vi.fn().mockRejectedValue(new ApiError(403, { detail: "forbidden" }));
    renderButton({ startOAuth });

    fireEvent.click(screen.getByRole("button", { name: /connect google workspace/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/only organization admins can manage connections/i);
  });
});
