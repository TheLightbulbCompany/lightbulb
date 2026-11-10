import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDel = vi.fn();
const refresh = vi.fn();
const billing = vi.hoisted(() => ({ price: 7 as number | null | undefined, frontDesk: false, subscribed: true }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, post: mockPost, del: mockDel }) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { front_desk_price_usd: billing.price, front_desk: billing.frontDesk, is_subscribed: billing.subscribed }, refresh }),
}));
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: { id: "org_1" }, membership: { role: "org:admin" } }),
}));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: vi.fn(() => () => {}) }) }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [] }) }));
vi.mock("@/lib/shellNav", () => ({ replaceSearchParams: vi.fn(), navigateShell: vi.fn() }));

import { MailPanel } from "../MailPanel";

const page = { address: "agents@acme.thelightbulb.company", meetings: [], messages: [], next: null };
let line: { number: string; myPhone?: string | null } | null;
let missing: "pending" | "absent";
const assign = vi.fn();

function renderMail() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><MailPanel item={null} /></SWRConfig>);
}

describe("MailPanel number", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    billing.price = 7;
    billing.frontDesk = false;
    billing.subscribed = true;
    line = null;
    missing = "absent";
    mockGet.mockImplementation(async (url: string) => {
      if (url === "/agent-text/line") {
        if (!line) throw new ApiError(404, { status: missing });
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

  it("offers the Front desk add-on only to a workspace already on the old plan", async () => {
    billing.subscribed = false; // a new voice buyer: the voice plan is the only thing offered
    renderMail();
    await waitFor(() => expect(mockGet.mock.calls.some(([u]) => u === "/agent-text/line")).toBe(true));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("button", { name: /Get a phone number/ })).toBeNull();
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

  it("polls while the number is pending", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      missing = "pending";
      renderMail();
      expect(await screen.findByText("Setting up your number…")).toBeInTheDocument();
      const lineReads = () => mockGet.mock.calls.filter(([u]) => u === "/agent-text/line").length;
      const before = lineReads();
      await vi.advanceTimersByTimeAsync(10_000);
      await waitFor(() => expect(lineReads()).toBeGreaterThan(before));
      expect(refresh).not.toHaveBeenCalled(); // a pending tick reads the line, never billing
      line = { number: "+15551230000" };
      await vi.advanceTimersByTimeAsync(10_000);
      expect(await screen.findByText("+15551230000")).toBeInTheDocument();
      await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1)); // the number arrived: billing once
      const after = lineReads();
      await vi.advanceTimersByTimeAsync(20_000);
      expect(lineReads()).toBe(after); // the line exists: polling stops
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling and refreshes billing once when the number is absent", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      missing = "pending";
      renderMail();
      expect(await screen.findByText("Setting up your number…")).toBeInTheDocument();
      const lineReads = () => mockGet.mock.calls.filter(([u]) => u === "/agent-text/line").length;
      missing = "absent";
      await vi.advanceTimersByTimeAsync(10_000);
      expect(await screen.findByRole("button", { name: /Get a phone number/ })).toBeInTheDocument();
      await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
      const after = lineReads();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(lineReads()).toBe(after);
      expect(refresh).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not poll without a pending number", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderMail();
      expect(await screen.findByRole("button", { name: /Get a phone number/ })).toBeInTheDocument();
      const before = mockGet.mock.calls.filter(([u]) => u === "/agent-text/line").length;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(mockGet.mock.calls.filter(([u]) => u === "/agent-text/line").length).toBe(before);
    } finally {
      vi.useRealTimers();
    }
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

  it("links a phone by code", async () => {
    line = { number: "+15551230000", myPhone: null };
    mockPost.mockResolvedValue({ code: "004821", number: "+15551230000", expiresAt: "2026-10-18T00:15:00+00:00" });
    renderMail();
    fireEvent.click(await screen.findByRole("button", { name: "Link my phone" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/agent-text/me/phone/code", {}));
    expect(await screen.findByText("LINK 004821")).toBeInTheDocument();
    expect(screen.getByText(/to \+15551230000 from your phone\. The code works for 15 minutes\./)).toBeInTheDocument();
    line = { number: "+15551230000", myPhone: "+15551234567" };
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Your phone: +15551234567")).toBeInTheDocument();
  });

  it("says when a code could not be had", async () => {
    line = { number: "+15551230000", myPhone: null };
    mockPost.mockRejectedValue(new ApiError(503, null));
    renderMail();
    fireEvent.click(await screen.findByRole("button", { name: "Link my phone" }));
    expect(await screen.findByText("Could not get a code")).toBeInTheDocument();
  });

  it("removes the member's own phone", async () => {
    line = { number: "+15551230000", myPhone: "+15551234567" };
    mockDel.mockResolvedValue({ status: "ok" });
    renderMail();
    expect(await screen.findByText("Your phone: +15551234567")).toBeInTheDocument();
    line = { number: "+15551230000", myPhone: null };
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith("/agent-text/me/phone"));
    expect(await screen.findByRole("button", { name: "Link my phone" })).toBeInTheDocument();
  });
});
