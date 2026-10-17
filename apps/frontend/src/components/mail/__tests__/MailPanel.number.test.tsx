import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const mockGet = vi.fn();
const mockPost = vi.fn();
const refresh = vi.fn();
const billing = vi.hoisted(() => ({ price: 7 as number | undefined }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, post: mockPost }) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { front_desk_price_usd: billing.price }, refresh }),
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

  it("shows the price from pricing, and neutral copy without one", async () => {
    const { unmount } = renderMail();
    expect(await screen.findByRole("button", { name: "Get a phone number · $7/month" })).toBeInTheDocument();
    unmount();
    billing.price = undefined;
    renderMail();
    expect(await screen.findByRole("button", { name: "Get a phone number" })).toBeInTheDocument();
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
});
