import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { apiPost, setView } = vi.hoisted(() => ({
  apiPost: vi.fn(),
  setView: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: vi.fn(),
    post: apiPost,
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    syncUser: vi.fn(),
    patchConfig: vi.fn(),
    uploadFiles: vi.fn(),
    saveWorkspaceFile: vi.fn(),
  }),
}));

vi.mock("@/components/marketplace/drawer/wizardSteps", () => ({
  PickAgentStep: ({ onSelect, selected }: { onSelect: (id: string) => void; selected?: string }) => (
    <div>
      <span>step1</span>
      <button onClick={() => onSelect("agent_pulse")}>pick</button>
      {selected && <span>selected={selected}</span>}
    </div>
  ),
  DetailsStep: ({ form }: { form: { name: string; tagline: string } }) => (
    <div>step2 — name={form.name} tag={form.tagline}</div>
  ),
  BundleStep: () => <div>step3-bundle</div>,
  PricingStep: () => <div>step4-pricing</div>,
  ReviewStep: () => <div>step5-review</div>,
  SuccessStep: () => <div>step6-success</div>,
}));

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ setView, state: { publishAgentId: undefined } }),
}));

import { PublishWizard } from "@/components/marketplace/drawer/PublishWizard";

describe("PublishWizard — submit chain", () => {
  beforeEach(() => {
    apiPost.mockReset();
    setView.mockReset();
  });

  it("3-call submit chain calls listings → artifact → submit, then advances to success", async () => {
    apiPost
      .mockResolvedValueOnce({ listing_id: "list_42" })
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true });

    const user = userEvent.setup();
    render(<PublishWizard />);

    // Drive through the wizard: pick agent → review → submit
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 2 (Details)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 3 (Bundle)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 4 (Pricing)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 5 (Review)
    await user.click(screen.getByRole("button", { name: /submit for review/i }));

    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(3));
    expect(apiPost).toHaveBeenNthCalledWith(
      1,
      "/marketplace/listings",
      expect.objectContaining({
        format: "openclaw",
        price_cents: 0,
        category: "operations",
      }),
    );
    expect(apiPost.mock.calls[1][0]).toBe("/marketplace/listings/list_42/artifact-from-agent");
    // Empty default-bundle is still sent so the backend can branch on
    // selection presence without sniffing for the key.
    expect(apiPost.mock.calls[1][1]).toMatchObject({
      agent_id: "agent_pulse",
      bundle: { mcps: [], skills: [] },
    });
    expect(apiPost.mock.calls[2][0]).toBe("/marketplace/listings/list_42/submit");

    await waitFor(() => expect(screen.getByText("step6-success")).toBeInTheDocument());
  });

  it("on 409 slug collision, surfaces error and returns to Step 2 without preserving draft id", async () => {
    const slugError = Object.assign(new Error("slug already taken"), {
      status: 409,
      detail: "slug already taken",
    });
    apiPost.mockRejectedValueOnce(slugError);

    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 2
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 3 (Bundle)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 4 (Pricing)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 5 (Review)
    await user.click(screen.getByRole("button", { name: /submit for review/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/slug is already taken/i));
    // Wizard should rewind to step 2 so the user can rename
    expect(screen.getByText(/^step2/)).toBeInTheDocument();
  });

  it("after step 1 succeeds but step 2 fails, retry skips listing creation", async () => {
    const snapshotError = Object.assign(new Error("snapshot failed"), {
      status: 500,
      detail: "snapshot failed",
    });
    apiPost
      .mockResolvedValueOnce({ listing_id: "list_99" }) // create draft
      .mockRejectedValueOnce(snapshotError) // first artifact-from-agent fails
      .mockResolvedValueOnce({ ok: true }) // retry artifact-from-agent succeeds
      .mockResolvedValueOnce({ ok: true }); // submit succeeds

    const user = userEvent.setup();
    render(<PublishWizard />);
    await user.click(screen.getByText("pick"));
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 2
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 3 (Bundle)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 4 (Pricing)
    await user.click(screen.getByRole("button", { name: /continue/i })); // → step 5 (Review)
    await user.click(screen.getByRole("button", { name: /submit for review/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/snapshot failed/i));

    // Retry — submit chain should resume from step 2 (artifact-from-agent), not recreate the draft
    await user.click(screen.getByRole("button", { name: /submit for review/i }));

    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(4));
    // First call was POST /marketplace/listings — only ONE call to that path overall
    const listingCreates = apiPost.mock.calls.filter((c) => c[0] === "/marketplace/listings");
    expect(listingCreates).toHaveLength(1);
    // Both artifact-from-agent and submit calls reuse the same listing_id
    expect(apiPost.mock.calls[2][0]).toBe("/marketplace/listings/list_99/artifact-from-agent");
    expect(apiPost.mock.calls[3][0]).toBe("/marketplace/listings/list_99/submit");
  });
});
