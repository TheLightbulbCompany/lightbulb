import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

import { ConnectButton } from "../ConnectButton";
import { ApiError, type Connection } from "@/hooks/useConnections";

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

const SWITCH_PROVIDER_HREF = "/chat?panel=llm&action=change-provider";

const googleRow: Connection = {
  provider: "google",
  display_name: "Google Workspace",
  acquisition: "direct_oauth",
  status: "disconnected",
  requires_bedrock: true,
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
  it("renders a Switch-to-Bedrock upsell (not Connect) when lockedForBedrock", () => {
    renderButton({ lockedForBedrock: true });

    const link = screen.getByRole("link", {
      name: /switch to bedrock to unlock google workspace/i,
    });
    expect(link).toHaveAttribute("href", SWITCH_PROVIDER_HREF);
    expect(screen.queryByRole("button", { name: /connect/i })).toBeNull();
  });

  it("disables the upsell CTA for non-admins (no actionable link into an admin-gated flow)", () => {
    renderButton({ lockedForBedrock: true, canManage: false });

    const btn = screen.getByRole("button", {
      name: /switch to bedrock to unlock google workspace/i,
    });
    expect(btn).toBeDisabled();
    expect(screen.queryByRole("link", { name: /switch to bedrock/i })).toBeNull();
  });

  it("renders Connect (not the upsell) when not locked", () => {
    renderButton({ lockedForBedrock: false });

    expect(screen.getByRole("button", { name: /connect google workspace/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /switch to bedrock/i })).toBeNull();
  });

  it("surfaces the structured requires_bedrock 403 message instead of the admin-denial copy", async () => {
    const startOAuth = vi.fn().mockRejectedValue(
      new ApiError(403, {
        detail: {
          reason: "requires_bedrock",
          message: "Google integrations require Bedrock-managed inference.",
          action_label: "Switch to Bedrock",
          action_href: SWITCH_PROVIDER_HREF,
        },
      }),
    );
    renderButton({ startOAuth });

    fireEvent.click(screen.getByRole("button", { name: /connect google workspace/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Google integrations require Bedrock-managed inference.");
    expect(alert).not.toHaveTextContent(/only organization admins/i);
  });

  it("still shows the admin-denial copy for a plain (non-Bedrock) 403", async () => {
    const startOAuth = vi.fn().mockRejectedValue(new ApiError(403, { detail: "forbidden" }));
    renderButton({ startOAuth });

    fireEvent.click(screen.getByRole("button", { name: /connect google workspace/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/only organization admins can manage connections/i);
  });
});
