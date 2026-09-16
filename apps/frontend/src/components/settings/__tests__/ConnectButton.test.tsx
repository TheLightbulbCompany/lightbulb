import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

import { ConnectButton } from "../ConnectButton";
import { ApiError, type Connection } from "@/hooks/useConnections";
import { openPanel } from "@/components/workspace/workspaceController";

// The Browser panel is a dock singleton; the button only asks for it.
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }));

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

  it("shows the admin-denial copy for a 403", async () => {
    const startOAuth = vi.fn().mockRejectedValue(new ApiError(403, { detail: "forbidden" }));
    renderButton({ startOAuth });

    fireEvent.click(screen.getByRole("button", { name: /connect google workspace/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/only organization admins can manage connections/i);
  });

  it("starts a container login with the typed store and surfaces the Browser panel", () => {
    // Shopify signs in INSIDE the container (loopback-bound OAuth callback), so
    // the button never redirects: it hands the store to the hook and opens the
    // panel the owner will finish in. Normalization is the server's job — only
    // whitespace is trimmed here.
    const startContainerLogin = vi.fn().mockResolvedValue(undefined);
    renderButton({
      connection: { provider: "shopify", display_name: "Shopify", acquisition: "container_login", status: "disconnected" },
      startContainerLogin,
    });
    fireEvent.change(screen.getByLabelText(/shopify store domain/i), { target: { value: " MyStore.myshopify.com " } });
    fireEvent.click(screen.getByRole("button", { name: /connect shopify/i }));
    expect(openPanel).toHaveBeenCalledWith("browser");
    expect(startContainerLogin).toHaveBeenCalledWith("shopify", "MyStore.myshopify.com");
    expect(screen.getByText(/sign in to shopify in the browser panel/i)).toBeInTheDocument();
  });
});
