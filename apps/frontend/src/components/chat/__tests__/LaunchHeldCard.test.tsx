import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// LaunchHeldCard's own module imports `fetchLaunchInfo` from "@/lib/launch",
// which reads BACKEND_URL from "@/lib/api" — preserve every other export via
// importOriginal (mirrors src/__tests__/teams/RoutinesPanel.test.tsx) so that
// read doesn't throw and fall the card back to its loading state.
const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, useApi: () => ({ get, post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }) };
});

import type { BlockedPayload } from "@/hooks/useProvisioningState";
import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";
import { LaunchHeldCard } from "../LaunchHeldCard";

const blocked: BlockedPayload = {
  code: "launch_pending",
  title: "You're on the list",
  message: "Doors open Friday, September 18 at 4:00 PM ET. Sign back in then and Friday runs your idea.",
  action: { kind: "link", label: "See your spot", href: "/launch", admin_only: false },
  owner_role: "admin",
};

const me = (over: Partial<LaunchMe> = {}): LaunchMe => ({
  phase: "waitlist",
  waitlist: {
    position: 47,
    referral_code: "P9AB3CDE",
    in_comp_window: true,
    referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
    referrals_signed_up: 0,
    referrals_paid: 0,
    free_months_banked: 0,
  },
  activated: false,
  activated_at: null,
  entry: null,
  joined: false,
  has_key: false,
  can_join: false,
  reason: "phase_waitlist",
  idea: null,
  ...over,
});

describe("LaunchHeldCard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    get.mockReset();
    post.mockReset();
    localStorage.clear();
  });

  const okInfo = () =>
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ drop_at: "2026-09-18T16:00:00+00:00" }) }),
    );

  it("renders position, idea and the referral link, plus the gate's fine print", async () => {
    get.mockResolvedValue(me({ idea: "A meal-planning agent for busy parents" }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ drop_at: "2026-09-18T16:00:00+00:00" }) }),
    );
    render(<LaunchHeldCard blocked={blocked} />);
    expect(await screen.findByText(/#47/)).toBeInTheDocument();
    expect(screen.getByText(/A meal-planning agent for busy parents/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/ref=P9AB3CDE/)).toBeInTheDocument();
    expect(screen.getByText(/Doors open Friday/)).toBeInTheDocument();
  });

  it("shows the 'add your idea' fallback when there is no pending idea", async () => {
    get.mockResolvedValue(me({ idea: null }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ drop_at: "2026-09-18T16:00:00+00:00" }) }),
    );
    render(<LaunchHeldCard blocked={blocked} />);
    expect(await screen.findByText(/Add your idea on drop day/)).toBeInTheDocument();
  });

  it("attributes a stored referral without a trip to /launch, and clears it on a resolved response", async () => {
    get.mockResolvedValue(me());
    post.mockResolvedValue({ attributed: false });
    okInfo();
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    render(<LaunchHeldCard blocked={blocked} />);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
  });

  it("does NOT clear the stored referral when the request throws", async () => {
    get.mockResolvedValue(me());
    post.mockRejectedValue(new Error("network blip"));
    okInfo();
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    render(<LaunchHeldCard blocked={blocked} />);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("K7QX2MPP");
  });

  it("falls back to the gate copy when /launch/me fails, instead of loading forever", async () => {
    get.mockRejectedValue(new Error("500"));
    okInfo();
    render(<LaunchHeldCard blocked={blocked} />);
    expect(await screen.findByText("You're on the list")).toBeInTheDocument();
    expect(screen.queryByText(/Loading your status/)).not.toBeInTheDocument();
  });
});
