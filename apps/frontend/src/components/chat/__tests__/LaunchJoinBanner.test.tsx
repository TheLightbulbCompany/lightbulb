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

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { id: "user_1", firstName: "Ada", primaryEmailAddress: { emailAddress: "ada@lovelace.dev" } },
    isLoaded: true,
  }),
}));

import type { LaunchMe } from "@/lib/launch";
import { LaunchJoinBanner } from "../LaunchJoinBanner";

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
  onboarded: true,
  display_name: null,
  suggested_display_name: null,
  entry: null,
  ...over,
});

const joined = (over: Partial<LaunchMe> = {}) =>
  me({
    joined: true,
    position: 7,
    effective_position: 7,
    referral_code: "X",
    referral_link: "https://thelightbulb.company/?ref=X",
    compete_eligible: true,
    ...over,
  });

function mount() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><LaunchJoinBanner /></SWRConfig>);
}

describe("LaunchJoinBanner", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    launchInfo.mockReturnValue(info());
    localStorage.clear();
  });

  it("shows the sheet position and referral link once joined", async () => {
    get.mockResolvedValue(joined({ effective_position: 41, referral_link: "https://thelightbulb.company/?ref=P9AB3CDE" }));
    mount();
    expect(await screen.findByText(/You're #41 on the launch sheet/)).toBeInTheDocument();
    expect(screen.getByText("https://thelightbulb.company/?ref=P9AB3CDE")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  it("never asks an unjoined owner to join — that is the modal's job now", async () => {
    // Regression: the strip used to carry the whole join form behind an
    // `onboarded` gate, so it was invisible during provisioning and then only
    // appeared after a full page reload (/launch/me is read once per load and
    // focus revalidation is off app-wide). Owners reported never seeing it.
    get.mockResolvedValue(me());
    const { container } = mount();
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByLabelText(/display name/i)).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows a joined owner whose bootstrap hasn't delivered — joining no longer needs onboarding", async () => {
    get.mockResolvedValue(joined({ onboarded: false }));
    mount();
    expect(await screen.findByText(/You're #7 on the launch sheet/)).toBeInTheDocument();
  });

  it("renders nothing when the launch is off (404) or the sheet has closed", async () => {
    launchInfo.mockReturnValue(null);
    const { container, unmount } = mount();
    expect(container).toBeEmptyDOMElement();
    unmount();

    launchInfo.mockReturnValue(info({ phase: "closed" }));
    const closed = mount();
    expect(closed.container).toBeEmptyDOMElement();
    expect(get).not.toHaveBeenCalled(); // /launch/me isn't even fetched off-phase
  });

  it("Dismiss hides it via the per-user key", async () => {
    get.mockResolvedValue(joined());
    const { container } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /dismiss/i }));
    expect(container).toBeEmptyDOMElement();
    expect(localStorage.getItem("launch_sheet_banner_dismissed:user_1")).toBe("1");
  });

  it("stays hidden on the next mount once dismissed", async () => {
    localStorage.setItem("launch_sheet_banner_dismissed:user_1", "1");
    get.mockResolvedValue(joined());
    const { container } = mount();
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
