import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const update = vi.fn().mockResolvedValue(undefined);
// Mutable so a test can seed a PRIOR onboarding cycle's stale firstRun flags
// (account-wipe / reprovision scenario). Reset to {} in beforeEach so every
// other test sees a clean, first-time user. Read at useUser() call-time, same
// call-time-deref pattern as `update` above.
let unsafeMetadata: Record<string, unknown> = {};
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { unsafeMetadata, update },
    isLoaded: true,
  }),
}));

import { FirstRunFunnel } from "@/components/onboarding/FirstRunFunnel";

// RETRY_MS / MAX_RETRIES mirrored from FirstRunFunnel.tsx (not exported).
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

function enterGrowPathAndSubmit() {
  fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
  fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
    target: { value: "acme.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
}

beforeEach(() => {
  post.mockReset();
  update.mockReset().mockResolvedValue(undefined);
  unsafeMetadata = {};
});

describe("FirstRunFunnel", () => {
  it("walks grow path: choose -> enter url -> submit -> writes metadata", async () => {
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "acme.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/onboarding/brief", {
        path: "grow",
        input: "acme.com",
        detail: undefined,
      });
    });
    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.path).toBe("grow");
    });
  });

  it("resets stale started/completed from a prior provision so the new cycle's kickoff isn't suppressed", async () => {
    // Cross-provision staleness, verified live on dev: the user completed a
    // PREVIOUS first-run cycle (started/completed both true), then their
    // account/container was wiped and rebuilt. Clerk metadata is per-user
    // forever, so those flags survive — while the gate (keying off the fresh
    // container's onboarded:false) correctly re-shows this funnel. A brief
    // submit STARTS A NEW cycle, so it must clear the stale flags in the same
    // write; otherwise useFirstRunKickoff hard-skips on completed:true and the
    // new cycle's kickoff is permanently suppressed (thread reconciliation
    // can't rescue a completed-latched run).
    unsafeMetadata = { firstRun: { briefSubmitted: true, started: true, completed: true } };
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    enterGrowPathAndSubmit();

    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.started).toBe(false);
      expect(meta.completed).toBe(false);
    });
  });

  it("does not write briefSubmitted when the response body is not staged/already_submitted", async () => {
    // Regression: the backend's `_ctx` dependency can return HTTP 202 while
    // Paperclip is still provisioning. The api client treats any 2xx as a
    // resolved promise, so the funnel must check the BODY, not just that
    // the POST resolved -- otherwise it writes briefSubmitted:true with
    // nothing staged, permanently bricking the show.
    vi.useFakeTimers();
    try {
      post.mockResolvedValue({ status: "provisioning" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
      await vi.waitFor(() =>
        expect(screen.getByRole("button", { name: /setting things up/i })).toBeInTheDocument(),
      );
      expect(update).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries an unexpected response body exactly like a 409, then lands in the error state", async () => {
    vi.useFakeTimers();
    try {
      post.mockResolvedValue({ detail: "still working" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));

      // 5 retries at RETRY_MS apart before the funnel gives up on attempt
      // MAX_RETRIES (6 total POSTs).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MAX_RETRIES * RETRY_MS + 1000);
      });

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(MAX_RETRIES + 1));
      await vi.waitFor(() =>
        expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
      );
      expect(update).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("submits with no input when the user says they don't know yet", async () => {
    // The absent idea IS the signal: BRIEF.md then tells Friday to look them
    // up and pick one. Sending "" instead would brief her with an empty quote.
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    // Enabled on an empty textarea — that is the whole point of it.
    fireEvent.click(screen.getByRole("button", { name: /don.t know yet/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/onboarding/brief", {
        path: "new",
        input: undefined,
        detail: undefined,
      });
    });
    await waitFor(() => {
      expect(update.mock.calls[0][0].unsafeMetadata.firstRun.briefSubmitted).toBe(true);
    });
  });

  it("offers the you-pick button on the new path only", () => {
    render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    // A grow user already has a business — there is nothing to pick for them,
    // and the backend 422s a grow brief with no website.
    expect(screen.queryByRole("button", { name: /don.t know yet/i })).toBeNull();
  });

  it("retries the no-idea submit as a no-idea submit", async () => {
    // Regression guard: Retry must replay the SAME submission. Replaying it as
    // a normal one would hit the >=3-char guard against an empty textarea and
    // silently do nothing, stranding the user on the error state forever.
    vi.useFakeTimers();
    try {
      post.mockRejectedValue({ status: 409 });
      render(<FirstRunFunnel />);
      fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
      fireEvent.click(screen.getByRole("button", { name: /don.t know yet/i }));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(MAX_RETRIES * RETRY_MS + 1000);
      });
      await vi.waitFor(() =>
        expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
      );

      post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
      fireEvent.click(screen.getByRole("button", { name: /retry/i }));

      await vi.waitFor(() => {
        expect(post).toHaveBeenLastCalledWith("/onboarding/brief", {
          path: "new",
          input: undefined,
          detail: undefined,
        });
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers no skip — the funnel is the committed onboarding front door", () => {
    render(<FirstRunFunnel />);
    // Path-picker screen: no skip.
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    // ...and none on the input screen either.
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
  });
});
