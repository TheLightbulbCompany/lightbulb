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

  it("renders labelled inputs for a multi-field paste provider and posts them as fields", async () => {
    // Shopify Dev Dashboard apps give a store domain + Client ID + Client secret.
    // Nobody types the packed "<shop>|<id>:<secret>" form — the server joins it.
    const connectPaste = vi.fn().mockResolvedValue(undefined);
    renderButton({
      connectPaste,
      connection: {
        provider: "shopify",
        display_name: "Shopify",
        acquisition: "paste_token",
        status: "disconnected",
        token_setup_url: "https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens",
        paste_fields: [
          { key: "shop", label: "Store domain", placeholder: "mystore.myshopify.com" },
          { key: "client_id", label: "Client ID" },
          { key: "client_secret", label: "Client secret", secret: true },
        ],
      },
    });

    const connect = screen.getByRole("button", { name: /connect shopify/i });
    expect(connect).toBeDisabled();
    expect(screen.getByLabelText("Client secret")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Store domain")).toHaveAttribute("placeholder", "mystore.myshopify.com");
    expect(screen.getByRole("link", { name: /where to find these/i })).toHaveAttribute(
      "href",
      "https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens",
    );

    fireEvent.change(screen.getByLabelText("Store domain"), { target: { value: "mystore.myshopify.com" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "cid" } });
    fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "shpss_x" } });
    fireEvent.click(connect);

    await waitFor(() =>
      expect(connectPaste).toHaveBeenCalledWith("shopify", {
        shop: "mystore.myshopify.com",
        client_id: "cid",
        client_secret: "shpss_x",
      }),
    );
  });

  it("shows the admin-denial copy for a 403", async () => {
    const startOAuth = vi.fn().mockRejectedValue(new ApiError(403, { detail: "forbidden" }));
    renderButton({ startOAuth });

    fireEvent.click(screen.getByRole("button", { name: /connect google workspace/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/only organization admins can manage connections/i);
  });
});
