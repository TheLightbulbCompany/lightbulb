import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

// `@/lib/notify` imports ApiError from the mocked `@/lib/api`; stub it the
// same way CompanyStripeSection.test.tsx does.
const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

import {
  ProvisionedResourcesFacet,
  type ProvisionedResource,
  type ProvisioningWorkflow,
} from "@/components/settings/ProvisionedResources";

const RESOURCE: ProvisionedResource = {
  resource_id: "res_1",
  provider: "posthog",
  service: "analytics",
  status: "active",
  resource_name: "Northwind analytics",
  env_keys: ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
  level: "company",
};

// The two-phase tab: opened EMPTY and synchronously in the click handler, then
// pointed at the URL the API mints (a `window.open` after an `await` is
// blocked by Safari).
function fakeTab() {
  return { opener: undefined, location: { href: "" }, close: vi.fn() };
}

function renderFacet(
  opts: {
    canManage?: boolean;
    resources?: ProvisionedResource[];
    workflows?: ProvisioningWorkflow[];
  } = {},
) {
  return render(
    <ProvisionedResourcesFacet
      provider="posthog"
      resources={opts.resources ?? [RESOURCE]}
      workflows={opts.workflows ?? []}
      canManage={opts.canManage ?? true}
      onChanged={vi.fn()}
    />,
  );
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  notifyApiError.mockReset();
  notifyError.mockReset();
});

describe("ProvisionedResourcesFacet", () => {
  it("names each resource and states its level", () => {
    renderFacet();
    expect(screen.getByText("Northwind analytics")).toBeInTheDocument();
    expect(screen.getByText("analytics")).toBeInTheDocument();
    expect(screen.getByText("Company-level")).toBeInTheDocument();
  });

  it("the dashboard link is minted on click, opened, and never rendered as an href", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockResolvedValue({ url: "https://app.posthog.com/x", expires_time: "2026-08-29T12:00:00Z" });
    renderFacet();

    const button = screen.getByRole("button", { name: "Open dashboard" });
    // Never an <a href>: the minted URL is bearer-like and must not be stored.
    expect(button.tagName).toBe("BUTTON");
    fireEvent.click(button);
    // Synchronously, before the API promise settles.
    expect(window.open).toHaveBeenCalledWith("", "_blank");

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/resources/posthog/res_1/dashboard-link", {}),
    );
    await waitFor(() => expect(tab.location.href).toBe("https://app.posthog.com/x"));
    expect(document.querySelector('a[href*="posthog.com/x"]')).toBeNull();
  });

  it("closes the pending tab when the mint fails", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockRejectedValue(new Error("409"));
    renderFacet();
    fireEvent.click(screen.getByRole("button", { name: "Open dashboard" }));
    await waitFor(() => expect(tab.close).toHaveBeenCalled());
    expect(notifyApiError).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
  });

  it("does not call the API when the popup is blocked", async () => {
    vi.spyOn(window, "open").mockImplementation(() => null);
    renderFacet();
    fireEvent.click(screen.getByRole("button", { name: "Open dashboard" }));
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
  });

  it("revoke deletes our copy and says the owner keeps the account", async () => {
    post.mockResolvedValue({ revoked: true });
    renderFacet();
    fireEvent.click(screen.getByRole("button", { name: "Revoke our copy" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/resources/posthog/res_1/revoke", {}),
    );
    expect(await screen.findByText(/You keep the account/)).toBeInTheDocument();
  });

  it("remove asks for the resource name and only enables on an exact match", async () => {
    renderFacet();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    const confirm = screen.getByRole("button", { name: "Remove for good" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type the name/), { target: { value: "wrong" } });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Type the name/), { target: { value: "Northwind analytics" } });
    expect(confirm).toBeEnabled();

    post.mockResolvedValue({ removed: true });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/resources/posthog/res_1/remove", {
        confirm: "Northwind analytics",
      }),
    );
  });

  it("the remove dialog states the billing and infrastructure effect", () => {
    renderFacet();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByText(/deletes it at posthog/i)).toBeInTheDocument();
    expect(screen.getByText(/stops any charges/i)).toBeInTheDocument();
  });

  it("offers a non-admin none of the three admin-and-paid routes", () => {
    renderFacet({ canManage: false });
    expect(screen.getByText("Northwind analytics")).toBeInTheDocument();
    // `dashboard-link`, `revoke` and `remove` all sit behind the router's
    // `Owner` gate (org admin + paid), so a member sees the row, not a 403.
    expect(screen.queryByRole("button", { name: "Open dashboard" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke our copy" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
  });

  it("chips a workflow the owner has not approved yet", () => {
    renderFacet({
      resources: [],
      workflows: [
        { workflow_id: "pwf_1", state: "awaiting_approval", provider: "posthog", service: "svc_analytics" },
      ],
    });
    expect(screen.getByText(/Waiting for your approval/)).toBeInTheDocument();
  });

  it("requires_review tells the owner to contact us and offers no button", () => {
    renderFacet({
      resources: [],
      workflows: [
        { workflow_id: "pwf_1", state: "requires_review", provider: "posthog", service: "svc_analytics" },
      ],
    });
    expect(screen.getByText(/needs a look from Lightbulb/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers Withdraw on an ask nothing was created for, and refreshes after", async () => {
    const onChanged = vi.fn();
    post.mockResolvedValue({ state: "cancelled" });
    render(
      <ProvisionedResourcesFacet
        provider="posthog"
        resources={[]}
        workflows={[
          { workflow_id: "pwf_1", state: "awaiting_approval", provider: "posthog", service: "svc_analytics" },
        ]}
        canManage
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/cancel", {}),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("offers no Withdraw once the request has started, or to a non-admin", () => {
    const started: ProvisioningWorkflow[] = [
      { workflow_id: "pwf_1", state: "provisioning", provider: "posthog", service: "svc_analytics" },
    ];
    const { unmount } = renderFacet({ resources: [], workflows: started });
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
    unmount();
    renderFacet({
      canManage: false,
      resources: [],
      workflows: [
        { workflow_id: "pwf_1", state: "awaiting_approval", provider: "posthog", service: "svc_analytics" },
      ],
    });
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
  });

  it("says why a withdrawal was refused rather than pretending it worked", async () => {
    post.mockRejectedValue(
      Object.assign(new Error("This request has already started — review it instead"), { status: 409 }),
    );
    renderFacet({
      resources: [],
      workflows: [{ workflow_id: "pwf_1", state: "stale", provider: "posthog", service: "svc_analytics" }],
    });
    fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
  });

  it("renders nothing at all when the provider has neither resources nor open work", () => {
    const { container } = renderFacet({ resources: [], workflows: [] });
    expect(container).toBeEmptyDOMElement();
  });
});
