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
  byo_required: true,
  byo_configured: false,
  byo_setup_url: "https://console.cloud.google.com/apis/credentials",
};

function renderByo(
  overrides: Partial<React.ComponentProps<typeof ConnectButton>> = {},
) {
  const startOAuth = vi.fn().mockResolvedValue(undefined);
  const refreshConnections = vi.fn().mockResolvedValue(undefined);
  render(
    <ConnectButton
      connection={googleByoRow}
      canManage
      connectPaste={vi.fn()}
      startOAuth={startOAuth}
      disconnect={vi.fn()}
      refreshConnections={refreshConnections}
      {...overrides}
    />,
  );
  return { startOAuth, refreshConnections };
}

describe("ConnectButton BYO branch", () => {
  beforeEach(() => {
    putMock.mockClear();
  });

  it("offers 'Use your own Google app' for a byo_required, not-configured provider (no lock needed)", () => {
    renderByo();
    expect(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    ).toBeInTheDocument();
  });

  it("exposes BYO setup for an owner already connected via the old shared-app flow (no destructive disconnect)", () => {
    renderByo({ connection: { ...googleByoRow, status: "connected", byo_configured: false } });
    // Disconnect stays available...
    expect(
      screen.getByRole("button", { name: /disconnect google workspace/i }),
    ).toBeInTheDocument();
    // ...AND the owner can store their own app without disconnecting first.
    expect(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    ).toBeInTheDocument();
  });

  it("does NOT show the BYO affordance once a connected owner has configured their own app", () => {
    renderByo({ connection: { ...googleByoRow, status: "connected", byo_configured: true } });
    expect(
      screen.getByRole("button", { name: /disconnect google workspace/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /use your own google workspace app/i }),
    ).not.toBeInTheDocument();
  });

  it("lets a disconnected owner with a stored app retry OR edit it (recover from a bad secret)", () => {
    renderByo({ connection: { ...googleByoRow, status: "disconnected", byo_configured: true } });
    // Retry the OAuth dance against the stored app...
    expect(
      screen.getByRole("button", { name: "Connect Google Workspace" }),
    ).toBeInTheDocument();
    // ...and an edit affordance to overwrite a bad stored app (not "use your own").
    const edit = screen.getByRole("button", { name: /edit your google workspace app/i });
    expect(edit).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /use your own google workspace app/i }),
    ).not.toBeInTheDocument();
    // Opening it reveals the client_id/secret form so the secret can be replaced.
    fireEvent.click(edit);
    expect(screen.getByLabelText(/client secret/i)).toBeInTheDocument();
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
    expect(screen.getByText(/limited use requirements/i)).toBeInTheDocument();
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

  it("refreshes the connections list after Save, before redirecting to OAuth", async () => {
    // After the PUT stores the app server-side, the connections list must be
    // re-fetched BEFORE startOAuth so `byo_configured` reflects the server. If
    // startOAuth then fails, the row leaves the BYO panel for the normal
    // Connect path instead of staying stuck.
    const { startOAuth, refreshConnections } = renderByo();
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );
    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "my-client-id" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "my-secret" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /save your own google workspace app/i }));

    await waitFor(() => expect(refreshConnections).toHaveBeenCalledTimes(1));
    // The refresh happens before the OAuth redirect is kicked off.
    expect(refreshConnections.mock.invocationCallOrder[0]).toBeLessThan(
      startOAuth.mock.invocationCallOrder[0],
    );
  });

  it("clears the typed inputs when Cancel is clicked", () => {
    // A typed client secret must not persist in React state — Cancel scrubs the
    // fields so reopening the panel shows empty inputs, not the prior secret.
    renderByo();
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );
    fireEvent.change(screen.getByLabelText(/client id/i), {
      target: { value: "typed-id" },
    });
    fireEvent.change(screen.getByLabelText(/client secret/i), {
      target: { value: "typed-secret" },
    });
    fireEvent.click(screen.getByRole("checkbox"));

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    // Panel closed back to the affordance button.
    expect(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    ).toBeInTheDocument();

    // Reopen — the inputs and acknowledgment are back to empty/unchecked.
    fireEvent.click(
      screen.getByRole("button", { name: /use your own google workspace app/i }),
    );
    expect(screen.getByLabelText(/client id/i)).toHaveValue("");
    expect(screen.getByLabelText(/client secret/i)).toHaveValue("");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("renders the normal Connect path for a configured-but-not-connected row", () => {
    // Once an owner has STORED a BYO app (`byo_configured: true`), the BYO panel
    // condition (`byo_required && !byo_configured`) no longer matches, so the row
    // falls through to the normal OAuth Connect affordance.
    const configuredRow: Connection = {
      ...googleByoRow,
      byo_configured: true,
    };
    render(
      <ConnectButton
        connection={configuredRow}
        canManage
        connectPaste={vi.fn()}
        startOAuth={vi.fn().mockResolvedValue(undefined)}
        disconnect={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: /connect google workspace/i }),
    ).toBeInTheDocument();
    // No BYO "use your own app" dead-end once a BYO app is already stored.
    expect(
      screen.queryByRole("button", { name: /use your own google workspace app/i }),
    ).toBeNull();
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
