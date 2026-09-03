import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
  ApiError: class extends Error {
    constructor(public status: number, public body: unknown, message?: string) { super(message); }
  },
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const launchInfo = vi.fn<() => unknown>();
vi.mock("@/components/launch/useLaunchInfo", () => ({ useLaunchInfo: () => launchInfo() }));

const provisioning = vi.fn<() => unknown>();
vi.mock("@/hooks/useProvisioningState", () => ({ useProvisioningState: () => provisioning() }));

const orgId = vi.fn<() => string | null>(() => null);
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: { id: "user_1" }, isLoaded: true }),
  useAuth: () => ({ orgId: orgId() }),
}));

import type { LaunchMe } from "@/lib/launch";
import { LaunchJoinModal } from "../LaunchJoinModal";

const info = (over: Record<string, unknown> = {}) => ({
  phase: "open",
  sheet_closes_at: "2026-09-30T16:00:00+00:00",
  competition_start_at: "2026-10-01T16:00:00+00:00",
  competition_end_at: "2026-12-01T16:00:00+00:00",
  signups: 40,
  comp_slots: 200,
  referral_jump: 10,
  ...over,
});

const me = (over: Partial<LaunchMe> = {}): LaunchMe => ({
  phase: "open",
  joined: false,
  position: null,
  effective_position: null,
  referrals_subscribed: 0,
  referral_code: null,
  referral_link: null,
  credits_milestone_reached: 0,
  winner: false,
  compete_eligible: false,
  onboarded: false, // the point: still provisioning
  display_name: null,
  suggested_display_name: null,
  entry: null,
  ...over,
});

/** `/container/status` shape, reduced to the one field this component reads. */
const container = (funnelState: string) => ({ container: { funnel: { state: funnelState } } });

function mount() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><LaunchJoinModal /></SWRConfig>);
}

describe("LaunchJoinModal", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    launchInfo.mockReturnValue(info());
    provisioning.mockReturnValue(container("complete"));
    orgId.mockReturnValue(null);
    localStorage.clear();
  });

  it("asks an owner whose container is still provisioning", async () => {
    // The whole reason this component exists: `onboarded: false` here, and the
    // ask still renders. The banner it replaced was gated on that flag.
    get.mockResolvedValue(me({ onboarded: false }));
    mount();
    expect(await screen.findByText(/The launch competition is open/)).toBeInTheDocument();
    expect(screen.getByText(/first 200 businesses on the sheet get their first month free/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/business name/i)).toHaveAttribute("placeholder", "Your company name");
  });

  it("waits for the brief before opening, so it never covers the first-run funnel", async () => {
    // FirstRunFunnel is the committed onboarding front door and has no skip.
    // Opening over it would put a competition pitch on top of the one form
    // that actually starts the owner's workspace.
    provisioning.mockReturnValue(container("not_started"));
    get.mockResolvedValue(me());
    const { container: dom } = mount();
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
    expect(get).not.toHaveBeenCalled(); // not even fetched yet
  });

  it("never shows in an org context, where joining can only fail", async () => {
    // POST /launch/join refuses every org caller with org_not_supported.
    orgId.mockReturnValue("org_1");
    get.mockResolvedValue(me());
    const { container: dom } = mount();
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
    expect(get).not.toHaveBeenCalled();
  });

  it("keeps Join disabled until a valid (2+ char) name is typed", async () => {
    get.mockResolvedValue(me());
    mount();
    const input = await screen.findByLabelText(/business name/i);
    const btn = screen.getByRole("button", { name: /join the sheet/i });
    expect(btn).toBeDisabled();
    fireEvent.change(input, { target: { value: "N" } });
    expect(btn).toBeDisabled();
    fireEvent.change(input, { target: { value: "Northwind Ceramics" } });
    expect(btn).toBeEnabled();
  });

  it("joins and shows the position plus the referral link", async () => {
    get.mockResolvedValue(me());
    post.mockResolvedValue(
      me({
        joined: true,
        position: 41,
        effective_position: 41,
        referral_code: "P9AB3CDE",
        referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
        compete_eligible: true,
      }),
    );
    mount();
    fireEvent.change(await screen.findByLabelText(/business name/i), { target: { value: "Northwind Ceramics" } });
    fireEvent.click(screen.getByRole("button", { name: /join the sheet/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Northwind Ceramics", public: true }),
    );
    expect(await screen.findByText(/You're #41 on the sheet/)).toBeInTheDocument();
    expect(screen.getByText("https://flickmeon.ai/?ref=P9AB3CDE")).toBeInTheDocument();
  });

  it("surfaces a refusal instead of closing", async () => {
    const { ApiError } = await import("@/lib/api");
    get.mockResolvedValue(me());
    post.mockRejectedValue(new ApiError(409, { detail: { reason: "closed" } }, "API 409"));
    mount();
    fireEvent.change(await screen.findByLabelText(/business name/i), { target: { value: "Northwind Ceramics" } });
    fireEvent.click(screen.getByRole("button", { name: /join the sheet/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByLabelText(/business name/i)).toBeInTheDocument(); // still open
  });

  it("'Not now' records the ask and does not come back", async () => {
    get.mockResolvedValue(me());
    const first = mount();
    fireEvent.click(await screen.findByRole("button", { name: /not now/i }));
    expect(localStorage.getItem("launch_sheet_modal_seen:user_1")).toBe("1");
    first.unmount();

    const second = mount();
    await waitFor(() => expect(second.container).toBeEmptyDOMElement());
  });

  it("never re-asks someone who already joined", async () => {
    get.mockResolvedValue(me({ joined: true, position: 7, effective_position: 7 }));
    const { container: dom } = mount();
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
  });

  it("stays out of the way once the sheet has closed", async () => {
    launchInfo.mockReturnValue(info({ phase: "closed" }));
    get.mockResolvedValue(me());
    const { container: dom } = mount();
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
    expect(get).not.toHaveBeenCalled();
  });
});
