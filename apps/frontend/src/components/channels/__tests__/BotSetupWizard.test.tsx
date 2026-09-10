import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { BotSetupWizard } from "@/components/channels/BotSetupWizard";

const postMock = vi.fn();
const patchConfigMock = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({
    patchConfig: patchConfigMock,
    post: postMock,
  }),
}));

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpcMutation: () => vi.fn().mockResolvedValue({
    channelAccounts: { telegram: [{ connected: true }] },
  }),
}));

describe("BotSetupWizard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    patchConfigMock.mockResolvedValue({ status: "patched", owner_id: "user_test" });
    postMock.mockResolvedValue({ status: "linked", peer_id: "12345" });
  });

  it("reaches the token paste step from the create-mode intro", async () => {
    // Create mode opens on an intro step (create-your-bot instructions);
    // the token field lives one Next-click in, behind a Connect CTA.
    const user = userEvent.setup();
    render(
      <BotSetupWizard
        mode="create"
        provider="telegram"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/bot token/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByLabelText(/bot token/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeDisabled();
  });

  it("enables the Connect button once a token is typed", async () => {
    const user = userEvent.setup();
    render(
      <BotSetupWizard
        mode="create"
        provider="telegram"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /next/i }));
    await user.type(screen.getByLabelText(/bot token/i), "123:abcABC");
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });

  it("skips the token step in link-only mode", () => {
    render(
      <BotSetupWizard
        mode="link-only"
        provider="telegram"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/bot token/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/pairing code/i)).toBeInTheDocument();
  });

  it("shows friendly message on 404 code not found", async () => {
    const err: Error & { status?: number } = new Error("not found");
    err.status = 404;
    postMock.mockRejectedValueOnce(err);

    render(
      <BotSetupWizard
        mode="link-only"
        provider="telegram"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/pairing code/i), "BADCODE");
    await user.click(screen.getByRole("button", { name: /link/i }));
    await waitFor(() => {
      expect(screen.getByText(/code expired or already used/i)).toBeInTheDocument();
    });
  });

  it("shows friendly message on 409 peer already linked", async () => {
    const err: Error & { status?: number } = new Error("conflict");
    err.status = 409;
    postMock.mockRejectedValueOnce(err);

    render(
      <BotSetupWizard
        mode="link-only"
        provider="telegram"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/pairing code/i), "ABC12345");
    await user.click(screen.getByRole("button", { name: /link/i }));
    await waitFor(() => {
      expect(screen.getByText(/already linked to another member/i)).toBeInTheDocument();
    });
  });

  it("walks slack through manifest step then TWO token fields", async () => {
    // Slack opens on the manifest step, then Next reveals the two-token
    // paste step (App-Level Token + Bot User OAuth Token).
    const user = userEvent.setup();
    render(
      <BotSetupWizard
        mode="create"
        provider="slack"
        agentId="main"
        onComplete={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getAllByText(/manifest/i).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByLabelText(/app.level token/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/bot user oauth token/i)).toBeInTheDocument();
  });
});
