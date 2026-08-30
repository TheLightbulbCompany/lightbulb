import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LaunchMe, LaunchWaitlist } from "@/lib/launch";
import { LaunchStatusLines } from "../LaunchStatusLines";

const waitlist = (over: Partial<LaunchWaitlist> = {}): LaunchWaitlist => ({
  position: 47,
  effective_position: 47,
  moved_up: 0,
  referral_jump: 10,
  referral_code: "P9AB3CDE",
  in_comp_window: false,
  referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
  referrals_signed_up: 0,
  referrals_paid: 0,
  free_months_banked: 0,
  ...over,
});

const me = (w: LaunchWaitlist): LaunchMe => ({
  phase: "waitlist",
  waitlist: w,
  activated: false,
  activated_at: null,
  entry: null,
  joined: false,
  has_key: false,
  can_join: false,
  reason: null,
  idea: null,
});

describe("LaunchStatusLines", () => {
  it("shows the effective position, not the raw one, and always shows the jump-rate line", () => {
    render(
      <LaunchStatusLines
        me={me(waitlist({ position: 47, effective_position: 17, moved_up: 30, referral_jump: 10, referrals_signed_up: 3 }))}
        dropAt="2026-09-18T16:00:00+00:00"
      />,
    );
    expect(screen.getByText(/^You're #17 on the list\.$/)).toBeInTheDocument();
    expect(screen.getByText(/^Up 30 spots from 3 referrals\.$/)).toBeInTheDocument();
    expect(screen.getByText(/^Each referral moves you up 10 spots\.$/)).toBeInTheDocument();
  });

  it("omits the 'moved up' line when no referral has jumped the queue yet", () => {
    render(
      <LaunchStatusLines me={me(waitlist())} dropAt="2026-09-18T16:00:00+00:00" />,
    );
    expect(screen.getByText(/^You're #47 on the list\.$/)).toBeInTheDocument();
    expect(screen.queryByText(/Up \d+ spots from/)).not.toBeInTheDocument();
    expect(screen.getByText(/^Each referral moves you up 10 spots\.$/)).toBeInTheDocument();
  });
});
