// AgentGuardrails — what agents may do, and the kill switch. It came from the
// retired Accounts tab and now sits at the foot of Settings → Connections,
// under the one list of what the team is connected to.
//
// One approval mode (Ask / Auto / Never ask) replaced the four switches; Never
// ask and revoke each sit behind a typed word, and only admins change them.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

const get = vi.fn();
const patch = vi.fn();
const post = vi.fn();
const del = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, patch, post, del }),
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

let rules: { id: string; kind: string; key: string; value: string }[] = [];
const POLICY = { money_moves: true, outreach: true, publishing: true, provisioning: true, approval_mode: "auto" };

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
  rules = [{ id: "always-1", kind: "mail.send", key: "to", value: "priya@acme.com" }];
  get.mockImplementation(async (path: string) => (path === "/company/policy/always-allow" ? { rules } : { ...POLICY }));
  del.mockImplementation(async () => { rules = []; return { removed: "always-1" }; });
  patch.mockResolvedValue({ ...POLICY });
  post.mockResolvedValue({ revoked: ["workflows_cancelled:2"] });
});

describe("AgentGuardrails — approval mode", () => {
  it("shows the three modes with the stored one chosen", async () => {
    renderPanel();
    const auto = await screen.findByRole("radio", { name: /Auto/ });
    expect(auto).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Ask/ })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: /Never ask/ })).not.toBeChecked();
    expect(screen.queryAllByRole("switch")).toHaveLength(0); // the four Ask-me-first switches are gone
  });

  it("PATCHes the chosen mode", async () => {
    patch.mockResolvedValue({ ...POLICY, approval_mode: "ask" });
    renderPanel();
    fireEvent.click(await screen.findByRole("radio", { name: /^Ask/ }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/company/policy", { approval_mode: "ask" }));
    await waitFor(() => expect(screen.getByRole("radio", { name: /^Ask/ })).toBeChecked());
  });

  it("Never ask waits for the typed word and sends it", async () => {
    patch.mockResolvedValue({ ...POLICY, approval_mode: "never_ask" });
    renderPanel();
    fireEvent.click(await screen.findByRole("radio", { name: /Never ask/ }));
    expect(patch).not.toHaveBeenCalled();
    const input = await screen.findByLabelText("Type never ask to confirm");
    const confirm = screen.getByRole("button", { name: "Turn on Never ask" });
    expect(confirm).toBeDisabled();
    fireEvent.change(input, { target: { value: "never ask" } });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/company/policy", { approval_mode: "never_ask", confirm: "never ask" }),
    );
  });

  it("a failed save notifies and puts the choice back", async () => {
    patch.mockRejectedValue(new Error("403"));
    renderPanel();
    fireEvent.click(await screen.findByRole("radio", { name: /^Ask/ }));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("radio", { name: /Auto/ })).toBeChecked());
  });

  it("shows a non-admin the mode read-only, with no revoke control", async () => {
    canManage = false;
    renderPanel();
    expect(await screen.findByRole("radio", { name: /Auto/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Revoke everything" })).not.toBeInTheDocument();
    expect(screen.getByText("A workspace admin changes this.")).toBeInTheDocument();
  });
});

describe("AgentGuardrails — revoke everything", () => {
  async function openDialog() {
    renderPanel();
    await screen.findByRole("radio", { name: /Auto/ });
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

describe("Settings › Approvals — Always allowed", () => {
  it("lists each rule by action and target, and Remove drops it", async () => {
    renderPanel();
    const list = await screen.findByRole("list", { name: "Always allowed" });
    expect(list).toHaveTextContent("Email · priya@acme.com");
    fireEvent.click(screen.getByRole("button", { name: "Remove Email · priya@acme.com" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/company/policy/always-allow/always-1"));
    expect(await screen.findByText(/Nothing yet/)).toBeInTheDocument();
  });

  it("is not read for a non-admin (the route is admin-only)", async () => {
    canManage = false;
    renderPanel();
    await screen.findByRole("radio", { name: /Auto/ });
    expect(get).not.toHaveBeenCalledWith("/company/policy/always-allow");
    expect(screen.queryByRole("list", { name: "Always allowed" })).toBeNull();
  });
});
