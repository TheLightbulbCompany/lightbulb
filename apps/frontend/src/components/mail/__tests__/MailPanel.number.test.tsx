import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const mockGet = vi.fn();
const mockPost = vi.fn();
const refresh = vi.fn();
const billing = vi.hoisted(() => ({ price: 7 as number | null | undefined, frontDesk: false }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, post: mockPost }) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { front_desk_price_usd: billing.price, front_desk: billing.frontDesk }, refresh }),
}));
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: { id: "org_1" }, membership: { role: "org:admin" } }),
}));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: vi.fn(() => () => {}) }) }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [] }) }));
vi.mock("@/lib/shellNav", () => ({ replaceSearchParams: vi.fn(), navigateShell: vi.fn() }));

import { MailPanel } from "../MailPanel";

const page = { address: "agents@acme.thelightbulb.company", meetings: [], messages: [], next: null };
let line: { number: string } | null;
const assign = vi.fn();

function renderMail() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><MailPanel item={null} /></SWRConfig>);
}

describe("MailPanel number", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    billing.price = 7;
    billing.frontDesk = false;
    line = null;
    mockGet.mockImplementation(async (url: string) => {
      if (url === "/agent-text/line") {
        if (!line) throw new ApiError(404, "none");
        return line;
      }
      return page;
    });
    Object.defineProperty(window, "location", { value: { ...window.location, assign }, writable: true });
  });

  it("shows the number when the line exists", async () => {
    line = { number: "+15551230000" };
    renderMail();
    expect(await screen.findByText("+15551230000")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Get a phone number/ })).toBeNull();
  });

  it("shows the price from pricing", async () => {
    renderMail();
    expect(await screen.findByRole("button", { name: "Get a phone number · $7/month" })).toBeInTheDocument();
  });

  it("checkout status navigates to url", async () => {
    mockPost.mockResolvedValue({ status: "checkout", url: "https://checkout.test/s" });
    renderMail();
    fireEvent.click(await screen.findByRole("button", { name: /Get a phone number/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.test/s"));
    expect(mockPost).toHaveBeenCalledWith("/billing/front-desk", { return_to: "web" });
  });

  it("added status refetches the line", async () => {
    mockPost.mockResolvedValue({ status: "added" });
    renderMail();
    fireEvent.click(await screen.findByRole("button", { name: /Get a phone number/ }));
    const lineReads = () => mockGet.mock.calls.filter(([u]) => u === "/agent-text/line").length;
    const before = lineReads();
    await waitFor(() => expect(lineReads()).toBeGreaterThan(before));
    expect(refresh).toHaveBeenCalled();
  });

  it("hides the button without a price", async () => {
    billing.price = null;
    renderMail();
    await waitFor(() => expect(mockGet.mock.calls.some(([u]) => u === "/agent-text/line")).toBe(true));
    await new Promise((r) => setTimeout(r, 50)); // let the 404 settle
    expect(screen.queryByRole("button", { name: /Get a phone number/ })).toBeNull();
  });

  it("shows the pending state while the number is being set up", async () => {
    billing.frontDesk = true;
    renderMail();
    expect(await screen.findByText("Setting up your number…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Get a phone number/ })).toBeNull();
  });

  it("shows plain words on a failed post", async () => {
    const cases: [ApiError, string | null][] = [
      [new ApiError(409, { detail: "apple_managed" }), "Not available on this plan"],
      [new ApiError(409, { detail: "no_front_desk_agent" }), "This workspace has no Customer Operations agent"],
      [new ApiError(500, null), "Could not start checkout"],
    ];
    for (const [err, words] of cases) {
      mockPost.mockRejectedValue(err);
      const { unmount } = renderMail();
      fireEvent.click(await screen.findByRole("button", { name: /Get a phone number/ }));
      expect(await screen.findByText(words!)).toBeInTheDocument();
      unmount();
    }
    mockPost.mockRejectedValue(new ApiError(400, { detail: "front_desk_price_unconfigured" }));
    renderMail();
    fireEvent.click(await screen.findByRole("button", { name: /Get a phone number/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Get a phone number/ })).toBeNull());
    expect(screen.queryByText(/400|unconfigured/)).toBeNull();
  });
});
