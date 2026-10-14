// AgentGuardrails — what agents may do, and the kill switch. It came from the
// retired Accounts tab and now sits at the foot of Settings → Connections,
// under the one list of what the team is connected to.
//
// The guardrail switches are recorded, not enforced; the typed revoke
// confirmation and the admin gate are what this suite pins.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const get = vi.fn();
const patch = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, patch, post }),
  BACKEND_URL: "",
}));

const { notifyApiError, notifySuccess, notifyError } = vi.hoisted(() => ({
  notifyApiError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyError: vi.fn(),
}));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifySuccess, notifyError }));

let canManage = true;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({
  useIsWorkspaceAdmin: () => canManage,
}));

import { AgentGuardrails } from "../AgentGuardrails";

const POLICY = { money_moves: true, outreach: true, publishing: true, provisioning: true };

function renderPanel() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <AgentGuardrails />
    </SWRConfig>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  canManage = true;
  get.mockResolvedValue({ ...POLICY });
  patch.mockResolvedValue({ ...POLICY });
  post.mockResolvedValue({ revoked: ["workflows_cancelled:2"] });
});

describe("AgentGuardrails — guardrails (recorded, NOT enforced)", () => {
  it("says so in the UI, not just in a code comment", async () => {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.getByRole("note")).toHaveTextContent(/Recorded, not yet enforced/i);
  });

  it("PATCHes exactly the one key that was toggled", async () => {
    renderPanel();
    const sw = await screen.findByRole("switch", { name: /before moving money/i });
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch).toHaveBeenCalledWith("/company/policy", { money_moves: false });
  });

  it("a 422 notifies and rolls the switch back to its prior position", async () => {
    patch.mockRejectedValue(new Error("422 unknown capability keys"));
    renderPanel();
    const sw = await screen.findByRole("switch", { name: /before moving money/i });
    fireEvent.click(sw);
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: /before moving money/i })).toHaveAttribute(
        "aria-checked",
        "true",
      ),
    );
  });

  it("shows a non-admin the recorded state read-only, with no revoke control", async () => {
    canManage = false;
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Revoke everything" })).not.toBeInTheDocument();
    expect(screen.getByText("A workspace admin changes these.")).toBeInTheDocument();
  });
});

describe("AgentGuardrails — revoke everything", () => {
  async function openDialog() {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    fireEvent.click(screen.getByRole("button", { name: "Revoke everything" }));
    return screen.findByLabelText("Type revoke to confirm");
  }

  it("keeps confirm disabled until the input reads exactly `revoke`", async () => {
    const input = await openDialog();
    const confirm = screen
      .getAllByRole("button", { name: /^Revoke everything$/ })
      .find((b) => b.closest("[role=dialog]"))!;
    expect(confirm).toBeDisabled();
    fireEvent.change(input, { target: { value: "revok" } });
    expect(confirm).toBeDisabled();
    fireEvent.change(input, { target: { value: "revoke" } });
    expect(confirm).toBeEnabled();
  });

  it("posts the exact confirm string the backend demands", async () => {
    const input = await openDialog();
    fireEvent.change(input, { target: { value: "revoke" } });
    const confirm = screen
      .getAllByRole("button", { name: /^Revoke everything$/ })
      .find((b) => b.closest("[role=dialog]"))!;
    fireEvent.click(confirm);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/company/revoke-all", { confirm: "revoke" });
    expect(notifySuccess).toHaveBeenCalled();
  });

  it("names the connected apps the backend could not disconnect", async () => {
    post.mockResolvedValue({ revoked: ["workflows_cancelled:0", "connection:google"], failed: ["slack"] });
    const input = await openDialog();
    fireEvent.change(input, { target: { value: "revoke" } });
    fireEvent.click(
      screen.getAllByRole("button", { name: /^Revoke everything$/ }).find((b) => b.closest("[role=dialog]"))!,
    );
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(notifyError.mock.calls[0][0]).toMatch(/slack/);
    expect(notifySuccess).toHaveBeenCalledWith("Revoked 2 item(s).");
  });

  it("surfaces a failure instead of closing as if it worked", async () => {
    post.mockRejectedValue(new Error("500"));
    const input = await openDialog();
    fireEvent.change(input, { target: { value: "revoke" } });
    fireEvent.click(
      screen.getAllByRole("button", { name: /^Revoke everything$/ }).find((b) => b.closest("[role=dialog]"))!,
    );
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(screen.getByLabelText("Type revoke to confirm")).toBeInTheDocument();
  });
});
