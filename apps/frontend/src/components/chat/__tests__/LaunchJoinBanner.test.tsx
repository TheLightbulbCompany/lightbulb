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

// A `vi.fn()` (not a fixed object) so individual tests can vary firstName —
// used below to check the email-local-part fallback when it's absent. There
// is deliberately no company-name field here: nothing this banner already
// has mounted carries the owner's Paperclip company name (see the comment
// on `prefill` in LaunchJoinBanner.tsx), so the prefill chain stops at
// first name -> email local-part.
const clerkUser = vi.fn<() => { id: string; firstName: string | null; primaryEmailAddress: { emailAddress: string } }>();
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: clerkUser(), isLoaded: true }),
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
  entry: null,
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
    clerkUser.mockReturnValue({ id: "user_1", firstName: "Ada", primaryEmailAddress: { emailAddress: "ada@lovelace.dev" } });
    localStorage.clear();
  });

  it("shows the open-sheet nudge with the display name prefilled from the profile, labeled as public", async () => {
    get.mockResolvedValue(me());
    mount();
    expect(await screen.findByText(/launch competition is open — first 200 get a month free/)).toBeInTheDocument();
    expect(screen.getByLabelText(/display name/i)).toHaveValue("Ada");
    expect(screen.getByText(/shown on the public board/i)).toBeInTheDocument();
  });

  it("falls back to the email local-part when the profile has no first name", async () => {
    clerkUser.mockReturnValue({ id: "user_1", firstName: null, primaryEmailAddress: { emailAddress: "ada@lovelace.dev" } });
    get.mockResolvedValue(me());
    mount();
    expect(await screen.findByLabelText(/display name/i)).toHaveValue("ada");
  });

  it("renders nothing when the launch is off (404), closed, or the owner isn't onboarded", async () => {
    launchInfo.mockReturnValue(null);
    const { container, unmount } = mount();
    expect(container).toBeEmptyDOMElement();
    unmount();

    launchInfo.mockReturnValue(info({ phase: "closed" }));
    const closed = mount();
    expect(closed.container).toBeEmptyDOMElement();
    expect(get).not.toHaveBeenCalled(); // /launch/me isn't even fetched off-phase
    closed.unmount();

    launchInfo.mockReturnValue(info());
    get.mockResolvedValue(me({ onboarded: false }));
    const notOnboarded = mount();
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(notOnboarded.container).toBeEmptyDOMElement();
  });

  it("joins inline with the (editable) display name and flips to the position + referral link", async () => {
    get.mockResolvedValue(me());
    post.mockResolvedValue(me({
      joined: true,
      position: 41,
      effective_position: 41,
      referral_code: "P9AB3CDE",
      referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
      compete_eligible: true,
    }));
    mount();
    const input = await screen.findByLabelText(/display name/i);
    fireEvent.change(input, { target: { value: "Ada L" } });
    fireEvent.click(screen.getByRole("button", { name: /^join$/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Ada L", public: true }));
    expect(await screen.findByText(/You're #41 on the launch sheet/)).toBeInTheDocument();
    expect(screen.getByText("https://flickmeon.ai/?ref=P9AB3CDE")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  it("surfaces the org refusal copy instead of joining", async () => {
    const { ApiError } = await import("@/lib/api");
    get.mockResolvedValue(me());
    post.mockRejectedValue(new ApiError(409, { detail: { reason: "org_not_supported" } }, "API 409"));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /^join$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Switch to your personal workspace/);
  });

  it("already joined: shows the sheet line and Dismiss hides it via the per-user key", async () => {
    get.mockResolvedValue(me({ joined: true, position: 7, effective_position: 7, referral_link: "https://flickmeon.ai/?ref=X", referral_code: "X", compete_eligible: true }));
    const { container } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /dismiss/i }));
    expect(container).toBeEmptyDOMElement();
    expect(localStorage.getItem("launch_sheet_banner_dismissed:user_1")).toBe("1");
  });

  it("stays hidden on the next mount once dismissed", async () => {
    localStorage.setItem("launch_sheet_banner_dismissed:user_1", "1");
    get.mockResolvedValue(me({ joined: true, position: 7, effective_position: 7, compete_eligible: true }));
    const { container } = mount();
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
