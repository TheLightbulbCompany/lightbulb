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

const orgId = vi.fn<() => string | null | undefined>(() => null);
const authLoaded = vi.fn<() => boolean>(() => true);
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: { id: "user_1" }, isLoaded: true }),
  useAuth: () => ({ orgId: orgId(), isLoaded: authLoaded() }),
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
const container = (funnelState: string, over: Record<string, unknown> = {}) => ({
  container: { funnel: { state: funnelState }, runtime_phase: "ready", ...over },
});

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
    authLoaded.mockReturnValue(true);
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

  it.each([
    ["a null funnel object", { container: { funnel: null } }],
    ["a funnel object with no state", { container: { funnel: {} } }],
    ["no funnel key at all", { container: {} }],
  ])("treats %s as brief-NOT-done, so it can never open over FirstRunFunnel", async (_label, state) => {
    // `funnel` is optional on the wire (routers/container.py builds it with
    // contract.get("funnel")) and useProvisioningState hands back the raw cast,
    // never deriveUIPhase's EMPTY_FUNNEL fallback. A naive
    // `funnel?.state !== "not_started"` reads a missing funnel as "done".
    provisioning.mockReturnValue(state);
    get.mockResolvedValue(me());
    const { container: dom } = mount();
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
    expect(get).not.toHaveBeenCalled();
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
        referral_link: "https://thelightbulb.company/?ref=P9AB3CDE",
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
    expect(screen.getByText("https://thelightbulb.company/?ref=P9AB3CDE")).toBeInTheDocument();
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

  it("holds off until Clerk has resolved, so an org member never leaks a fetch", async () => {
    // useAuth() reports orgId: undefined until Clerk settles. Without the
    // isLoaded term, `!orgId` passes on the first frames and an org member —
    // who POST /launch/join refuses outright — fires a /launch/me they can
    // never act on.
    authLoaded.mockReturnValue(false);
    orgId.mockReturnValue(undefined);
    get.mockResolvedValue(me());
    const { container: dom } = mount();
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
    expect(get).not.toHaveBeenCalled();
  });

  it("still asks a legacy owner whose funnel never ran but who IS onboarded", async () => {
    // ProvisioningStepper documents this cohort: onboarded via the old
    // `onboarded_at` fact, funnel.state stuck at "not_started". Gating purely
    // on the funnel would lock them out permanently — and the banner no longer
    // carries a join form, so they would have no in-product path at all.
    provisioning.mockReturnValue(container("not_started", { onboarded: true }));
    get.mockResolvedValue(me({ onboarded: true }));
    mount();
    expect(await screen.findByText(/The launch competition is open/)).toBeInTheDocument();
  });

  it("does not tell an established owner their workspace is still building", async () => {
    provisioning.mockReturnValue(container("complete", { runtime_phase: "ready" }));
    get.mockResolvedValue(me({ onboarded: true }));
    mount();
    expect(await screen.findByText(/The launch competition is open/)).toBeInTheDocument();
    expect(screen.queryByText(/still building/i)).not.toBeInTheDocument();
  });

  it("does say so while the container really is building", async () => {
    provisioning.mockReturnValue(container("complete", { runtime_phase: "provisioning" }));
    get.mockResolvedValue(me());
    mount();
    expect(await screen.findByText(/still building/i)).toBeInTheDocument();
  });

  it("asks only ONCE against a backend that still refuses not_onboarded", async () => {
    // Deploy skew: Vercel ships this file to prod on merge, the backend that
    // drops the gate waits for a manual dispatch. Without burning the flag the
    // owner is re-prompted every single page load with copy this PR deleted.
    const { ApiError } = await import("@/lib/api");
    get.mockResolvedValue(me());
    post.mockRejectedValue(new ApiError(409, { detail: { reason: "not_onboarded" } }, "API 409"));
    const first = mount();
    fireEvent.change(await screen.findByLabelText(/business name/i), { target: { value: "Northwind Ceramics" } });
    fireEvent.click(screen.getByRole("button", { name: /join the sheet/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(localStorage.getItem("launch_sheet_modal_seen:user_1")).toBe("1"));
    first.unmount();

    const second = mount();
    await waitFor(() => expect(second.container).toBeEmptyDOMElement());
  });

  it("keeps asking after a retryable failure, which is not the skew case", async () => {
    const { ApiError } = await import("@/lib/api");
    get.mockResolvedValue(me());
    post.mockRejectedValue(new ApiError(429, { detail: "rate_limited" }, "API 429"));
    mount();
    fireEvent.change(await screen.findByLabelText(/business name/i), { target: { value: "Northwind Ceramics" } });
    fireEvent.click(screen.getByRole("button", { name: /join the sheet/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(localStorage.getItem("launch_sheet_modal_seen:user_1")).toBeNull();
  });

  it("trusts the authenticated phase over a stale public one", async () => {
    // useLaunchInfo fetches GET /launch once on mount and never revalidates,
    // so a tab open since before the sheet closed still reports "open".
    // /launch/me carries a fresher phase from a read this component already
    // makes — offering a join that could only 409 is worse than staying shut.
    launchInfo.mockReturnValue(info({ phase: "open" }));
    get.mockResolvedValue(me({ phase: "closed" }));
    const { container: dom } = mount();
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(dom).toBeEmptyDOMElement());
  });

  it("prefills the business name from the owner's real company name", async () => {
    // The backend only sends a suggestion for an ONBOARDED, non-org, unjoined
    // caller — i.e. only once Friday has renamed the company to something real
    // — so prefilling can't reintroduce the "person's name on a business board"
    // problem. Found live on dev: /launch/me returned "Toolchain" and this
    // dialog was still making the owner retype it.
    get.mockResolvedValue(me({ onboarded: true, suggested_display_name: "Toolchain" }));
    mount();
    expect(await screen.findByLabelText(/business name/i)).toHaveValue("Toolchain");
    expect(screen.getByRole("button", { name: /join the sheet/i })).toBeEnabled();
  });

  it("lets a typed edit beat the prefill, and posts what was typed", async () => {
    get.mockResolvedValue(me({ onboarded: true, suggested_display_name: "Toolchain" }));
    post.mockResolvedValue(me({ joined: true, position: 9, effective_position: 9 }));
    mount();
    const input = await screen.findByLabelText(/business name/i);
    fireEvent.change(input, { target: { value: "Northwind Ceramics" } });
    fireEvent.click(screen.getByRole("button", { name: /join the sheet/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Northwind Ceramics", public: true }),
    );
  });

  it("falls back to the placeholder when there is no suggestion to trust", async () => {
    get.mockResolvedValue(me({ onboarded: false, suggested_display_name: null }));
    mount();
    const input = await screen.findByLabelText(/business name/i);
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Your company name");
  });
});
