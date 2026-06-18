import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

import { ConnectButton } from "../ConnectButton";
import { type Connection } from "@/hooks/useConnections";

// Render next/link as a plain anchor so no router context is needed.
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

// Capture the BYO PUT so we can assert the endpoint + payload. The component
// reads `useApi().put` directly for the BYO-app store.
const putMock = vi.fn().mockResolvedValue({ status: "stored" });
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    useApi: () => ({ put: putMock, del: vi.fn(), get: vi.fn(), post: vi.fn() }),
  };
});

const googleByoRow: Connection = {
  provider: "google",
  display_name: "Google Workspace",
  acquisition: "direct_oauth",
  status: "disconnected",
  requires_bedrock: true,
  byo_capable: true,
  byo_configured: false,
  byo_setup_url: "https://console.cloud.google.com/apis/credentials",
};

function renderByo(
  overrides: Partial<React.ComponentProps<typeof ConnectButton>> = {},
) {
  const startOAuth = vi.fn().mockResolvedValue(undefined);
  render(
    <ConnectButton
      connection={googleByoRow}
      canManage
      lockedForBedrock
      connectPaste={vi.fn()}
      startOAuth={startOAuth}
      disconnect={vi.fn()}
      {...overrides}
    />,
  );
  return { startOAuth };
}

describe("ConnectButton BYO branch", () => {
  beforeEach(() => {
    putMock.mockClear();
  });

  it("offers 'Use your own Google app' for a byo_capable, locked, not-configured provider", () => {
    renderByo();
    expect(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    ).toBeInTheDocument();
    // The Bedrock upsell is replaced by the BYO affordance, not shown alongside.
    expect(screen.queryByRole("link", { name: /switch to bedrock/i })).toBeNull();
  });

  it("opens a panel with the setup link, redirect URI, inputs and acknowledgment", () => {
    renderByo();
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );

    // Setup-URL link.
    expect(screen.getByRole("link", { name: /register an oauth app/i })).toHaveAttribute(
      "href",
      "https://console.cloud.google.com/apis/credentials",
    );
    // Derived redirect URI (mirrors backend _redirect_uri).
    expect(screen.getByText(/\/oauth\/google\/callback$/)).toBeInTheDocument();
    // Inputs + acknowledgment.
    expect(screen.getByLabelText(/client id/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/client secret/i)).toBeInTheDocument();
    expect(screen.getByText(/limited use policy/i)).toBeInTheDocument();
  });

  it("PUTs the byo-app then proceeds to OAuth on Save", async () => {
    const { startOAuth } = renderByo();
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );

    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "my-client-id" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "my-secret" },
    });
    // Acknowledgment checkbox gates Save.
    const ack = screen.getByRole("checkbox");
    fireEvent.click(ack);

    fireEvent.click(screen.getByRole("button", { name: /save your own google workspace app/i }));

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    expect(putMock).toHaveBeenCalledWith("/connections/google/byo-app", {
      client_id: "my-client-id",
      client_secret: "my-secret",
      acknowledge: true,
    });
    await waitFor(() => expect(startOAuth).toHaveBeenCalledWith("google"));
  });

  it("disables Save until both fields and acknowledgment are present", () => {
    renderByo();
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );

    const save = screen.getByRole("button", { name: /save your own google workspace app/i });
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "id" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "secret" },
    });
    // Still disabled without the acknowledgment.
    expect(save).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(save).toBeEnabled();
  });
});
