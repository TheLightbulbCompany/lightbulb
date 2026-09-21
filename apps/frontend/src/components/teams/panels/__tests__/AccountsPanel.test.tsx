// AccountsPanel — the Company > Accounts tab.
//
// The two composed Settings surfaces (CompanyStripeSection, ConnectionsList)
// are stubbed: they have their own suites, and this one is about the panel's
// OWN behaviour — the guardrail switches (recorded, not enforced), the typed
// revoke confirmation, and the admin gate.
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

vi.mock("@/components/settings/CompanyStripeSection", () => ({
  CompanyStripeSection: ({ canManage }: { canManage: boolean }) => (
    <div data-testid="company-stripe">{String(canManage)}</div>
  ),
}));
vi.mock("@/components/settings/ConnectionsList", () => ({
  // Records `labels` so the panel's relabelling contract is assertable here;
  // that the override actually changes the rendered row is ConnectionsList's
  // own suite ("display-name overrides").
  ConnectionsList: ({ canManage, labels }: { canManage: boolean; labels?: Record<string, string> }) => (
    <div data-testid="connections-list" data-labels={JSON.stringify(labels ?? {})}>
      {String(canManage)}
    </div>
  ),
}));

let railProviders: string[] = [];
vi.mock("@/components/settings/ProvisionedResources", () => ({
  useProvisioningRail: () => ({
    byProvider: new Map(),
    workflowsByProvider: new Map(),
    accountByProvider: new Map(),
    error: null,
    providers: railProviders,
    refresh: vi.fn(),
  }),
}));

let connectionRows = [{ provider: "github", status: "connected", runtime_supported: true }];
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections: connectionRows }),
}));

let canManage = true;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({
  useIsWorkspaceAdmin: () => canManage,
}));

import { AccountsPanel } from "../AccountsPanel";

const POLICY = { money_moves: true, outreach: true, publishing: true, provisioning: true };

function renderPanel() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <AccountsPanel />
    </SWRConfig>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  canManage = true;
  railProviders = [];
  connectionRows = [{ provider: "github", status: "connected", runtime_supported: true }];
  get.mockResolvedValue({ ...POLICY });
  patch.mockResolvedValue({ ...POLICY });
  post.mockResolvedValue({ revoked: ["workflows_cancelled:2"] });
});

describe("AccountsPanel — masthead + composition", () => {
  it("renders the Company / Accounts masthead and both composed sections", async () => {
    renderPanel();
    expect(screen.getByRole("heading", { name: "Accounts", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Company")).toBeInTheDocument();
    expect(screen.getByTestId("company-stripe")).toBeInTheDocument();
    expect(screen.getByTestId("connections-list")).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/company/policy"));
  });

  it("counts connected OAuth providers and rail providers, deduped", async () => {
    railProviders = ["neon", "github"]; // github is also a connected OAuth row
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent("2 connected");
  });

  it("does not count an unsupported old grant as wired", async () => {
    connectionRows = [{ provider: "github", status: "connected", runtime_supported: false }];
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent("0 connected");
  });
});

describe("AccountsPanel — the company account and the agent grants are different rails", () => {
  // Regression, seen live on dev: this panel rendered a section literally
  // labelled "Connected" — holding a Stripe row reading "Not connected ·
  // OAuth" — directly under a company-account card reading "Connected as
  // acct_1UAL…". Two things called Stripe, opposite states, one screen. An
  // owner read the page as proof their Stripe integration was connected and
  // could not square it with a `no_company_stripe_account` failure.
  //
  // They ARE different rails: the company account is the owner's own Stripe
  // Connect merchant account (backend-only credential, revenue lands there),
  // the row is a direct_oauth MCP grant whose token lives in the container.
  // Each has to be labelled by what it does, and each has to disclaim being
  // the other.
  it("labels each rail by what it does rather than leaving the brand to do it", async () => {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    // Queried as the section triggers themselves, which also proves the query
    // below would have found the old "Connected" section had it survived.
    expect(screen.getByRole("button", { name: /^Where money lands/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^What your agents can use/ })).toBeInTheDocument();
    // The bare status word is no longer a section heading — it was the half of
    // the contradiction that made a not-connected row look authoritative.
    // Scoped to the PanelSection triggers rather than the whole page: the real
    // ConnectionsList legitimately renders "Connected" on a connected row, and
    // it is only stubbed here.
    expect(screen.queryByRole("button", { name: /^Connected/ })).not.toBeInTheDocument();
  });

  // The load-bearing one. Section framing alone still left the provider row
  // labelled with the bare word "Stripe" while carrying "Not connected" — and
  // an owner arriving from the auto-filed Stripe setup card reads that as "the
  // upgrade I just did failed", then clicks Connect on the MCP grant, which is
  // the wrong recovery: they end up OAuth-connected with still no company
  // account. The brand name alone must never be the label a connection status
  // hangs off, so the status can never be read as being about the money.
  it("never lets the bare brand name carry the provider row's status", async () => {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    const labels = JSON.parse(
      screen.getByTestId("connections-list").getAttribute("data-labels") ?? "{}",
    );
    expect(labels.stripe).toBeTruthy();
    expect(labels.stripe).not.toBe("Stripe");
    expect(labels.stripe).toMatch(/agent/i);
  });

  it("says on the page that neither rail is the other", async () => {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.getByText(/this is not an integration/i)).toBeInTheDocument();
    expect(screen.getByText(/none of them is your company account/i)).toBeInTheDocument();
  });
});

describe("AccountsPanel — provisioned empty state", () => {
  it("says nothing is provisioned yet when the rail is empty", async () => {
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.getByText("Nothing provisioned yet.")).toBeInTheDocument();
  });

  it("drops the reassurance once the rail has a provider", async () => {
    railProviders = ["neon"];
    renderPanel();
    await screen.findByText("Ask me first: Moving money");
    expect(screen.queryByText("Nothing provisioned yet.")).not.toBeInTheDocument();
  });
});

describe("AccountsPanel — guardrails (recorded, NOT enforced)", () => {
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

describe("AccountsPanel — revoke everything", () => {
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
