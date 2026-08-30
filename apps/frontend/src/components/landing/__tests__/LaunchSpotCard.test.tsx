import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiPost, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly body: unknown,
      message?: string,
    ) {
      super(message ?? `API ${status}`);
    }
  }
  return { apiPost: vi.fn(), FakeApiError };
});

vi.mock("@/lib/api", () => ({
  ApiError: FakeApiError,
  useApi: () => ({ get: vi.fn(), post: apiPost, put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

// The replay/attribution side is exercised through the shared hook in its own
// test file — here it is inert so the card's own idea form is what's on trial.
vi.mock("@/hooks/useLandingBriefReplay", () => ({
  LANDING_BRIEF_STORAGE_KEY: "isol8_landing_brief_v1",
  useLandingBriefReplay: () => ({ replaying: false, orgDraftVersion: 0 }),
}));

import type { LaunchInfo, LaunchMe } from "@/lib/launch";
import { LaunchSpotCard } from "../LaunchSpotCard";

const info: LaunchInfo = {
  phase: "waitlist",
  drop_at: "2026-09-18T16:00:00+00:00",
  competition_start_at: "2026-09-25T16:00:00+00:00",
  competition_end_at: "2026-10-25T16:00:00+00:00",
  signups: 1234,
  competing: 0,
  on_board: 0,
  pot_cents: 103000,
  comp_slots: 220,
  pot_per_entrant_cents: 1000,
  referral_jump: 10,
};

const me = (over: Partial<LaunchMe> = {}): LaunchMe => ({
  phase: "waitlist",
  waitlist: {
    position: 47,
    effective_position: 17,
    moved_up: 30,
    referral_jump: 10,
    referral_code: "P9AB3CDE",
    in_comp_window: false,
    referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
    referrals_signed_up: 3,
    referrals_paid: 0,
    free_months_banked: 0,
  },
  activated: false,
  activated_at: null,
  entry: null,
  joined: false,
  has_key: false,
  can_join: false,
  reason: null,
  idea: null,
  idea_full: null,
  ...over,
});

describe("LaunchSpotCard", () => {
  beforeEach(() => {
    apiPost.mockReset();
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it("no stored idea: the textarea is the centerpiece, and Save posts the brief", async () => {
    apiPost.mockResolvedValue({ status: "staged" });
    render(<LaunchSpotCard me={me()} info={info} />);
    expect(screen.getByText(/^You're #17 on the list\.$/)).toBeInTheDocument();
    expect(
      screen.getByText("Drop your idea now — Friday starts on it the second doors open."),
    ).toBeInTheDocument();
    // Same ET wall-time copy the backend's launch_hold gate renders.
    expect(
      screen.getByText("Doors open Friday, September 18 at 12:00 PM ET. Sign back in then and Friday runs your idea."),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Your idea"), {
      target: { value: "A ceramics studio that ships nationwide" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save idea" }));
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith("/onboarding/brief", {
        path: "new",
        input: "A ceramics studio that ships nationwide",
      }),
    );
    expect(await screen.findByText("A ceramics studio that ships nationwide")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("mirrors the backend floor: 1-2 non-blank chars disable Save", () => {
    render(<LaunchSpotCard me={me()} info={info} />);
    fireEvent.change(screen.getByLabelText("Your idea"), { target: { value: "ab" } });
    expect(screen.getByRole("button", { name: "Save idea" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Your idea"), { target: { value: "abc" } });
    expect(screen.getByRole("button", { name: "Save idea" })).toBeEnabled();
  });

  it("a stored idea shows with an Edit affordance; saving re-POSTs the new text", async () => {
    apiPost.mockResolvedValue({ status: "staged" });
    render(<LaunchSpotCard me={me({ idea: "Old idea", idea_full: "Old idea" })} info={info} />);
    expect(screen.getByText("Old idea")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Your idea");
    expect(box).toHaveValue("Old idea");
    fireEvent.change(box, { target: { value: "A sharper idea" } });
    fireEvent.click(screen.getByRole("button", { name: "Save idea" }));
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith("/onboarding/brief", { path: "new", input: "A sharper idea" }),
    );
    expect(await screen.findByText("A sharper idea")).toBeInTheDocument();
  });

  it("a 422 shows the server's detail string inline and keeps the form", async () => {
    apiPost.mockRejectedValue(
      new FakeApiError(422, { detail: "input must be at least 3 characters" }, "input must be at least 3 characters"),
    );
    render(<LaunchSpotCard me={me()} info={info} />);
    fireEvent.change(screen.getByLabelText("Your idea"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Save idea" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("input must be at least 3 characters");
    expect(screen.getByLabelText("Your idea")).toBeInTheDocument();
  });
});
