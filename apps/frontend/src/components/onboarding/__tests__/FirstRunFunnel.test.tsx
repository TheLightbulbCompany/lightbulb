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
  // The funnel persists an in-progress draft (see the remount tests below),
  // so every test must start from an empty one.
  window.sessionStorage.clear();
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

  it("re-arms briefSubmitted from a prior provision (account wipe / reprovision)", async () => {
    // Cross-provision staleness: the user completed a PREVIOUS first-run
    // cycle, then their account/container was wiped and rebuilt. Clerk
    // metadata is per-user forever, so `briefSubmitted` from that prior
    // cycle survives — while the gate (keying off the fresh container's
    // onboarded:false / funnel_state on the backend) correctly re-shows this
    // funnel. A brief submit re-arms `briefSubmitted` for the NEW cycle. The
    // kickoff itself is server-owned now (spec PR-6) and fenced on the
    // backend's own onboarding_generation, so there is no client-side
    // started/completed latch left to reset here.
    unsafeMetadata = { firstRun: { briefSubmitted: true } };
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    enterGrowPathAndSubmit();

    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.path).toBe("grow");
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

  it("switches to a persistent error with upstream detail after 3 identical failures", async () => {
    // Non-409 failures skip the internal retry loop, so each submit is one
    // recorded failure. Same upstream detail three times in a row ⇒ the
    // implied-transient "Retry" line gives way to a persistent state that
    // surfaces the detail and routes to support.
    post.mockRejectedValue({ status: 500, body: { detail: "paperclip exploded" } });
    render(<FirstRunFunnel />);
    enterGrowPathAndSubmit();

    // Failure 1 — generic transient copy with Retry.
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );

    // Failure 2 — still generic.
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );

    // Failure 3 — persistent: upstream detail + support, no Retry.
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByText(/we couldn.t save your brief/i)).toBeInTheDocument(),
    );
    expect(screen.getByText("paperclip exploded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    const support = screen.getByRole("link", { name: /contact support/i });
    expect(support.getAttribute("href")).toBe("mailto:support@isol8.co");
  });

  it("a different upstream error resets the failure streak", async () => {
    post.mockRejectedValueOnce({ status: 500, body: { detail: "error A" } });
    post.mockRejectedValueOnce({ status: 500, body: { detail: "error A" } });
    post.mockRejectedValueOnce({ status: 500, body: { detail: "error B" } });
    render(<FirstRunFunnel />);
    enterGrowPathAndSubmit();

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );
    // Third failure carries a DIFFERENT detail — streak resets to 1, so the
    // generic transient copy (with Retry) stays.
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/we couldn.t save your brief/i)).toBeNull();
  });

  it("restores an in-progress draft after a remount", () => {
    const { unmount } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "acme.com" },
    });

    // The funnel does not control its own lifetime: ProvisioningStepper
    // swaps it for a loader whenever /container/status is momentarily
    // unreadable, which unmounts it mid-typing. Chosen path AND typed text
    // must both survive that, or the user is dropped back on the two
    // options having lost their work.
    unmount();
    render(<FirstRunFunnel />);

    expect(screen.getByPlaceholderText(/yourcompany\.com/i)).toHaveValue("acme.com");
  });

  it("restores the new-business draft including the optional audience field", () => {
    const { unmount } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    fireEvent.change(screen.getByPlaceholderText(/social media agency/i), {
      target: { value: "A bakery for dogs" },
    });
    fireEvent.change(screen.getByPlaceholderText(/who.s it for/i), {
      target: { value: "Dog owners" },
    });

    unmount();
    render(<FirstRunFunnel />);

    expect(screen.getByPlaceholderText(/social media agency/i)).toHaveValue("A bakery for dogs");
    expect(screen.getByPlaceholderText(/who.s it for/i)).toHaveValue("Dog owners");
  });

  it("drops the draft once the brief is submitted", async () => {
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    const { unmount } = render(<FirstRunFunnel />);
    enterGrowPathAndSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    // A submitted brief is done. A container wipe / reprovision legitimately
    // re-shows the funnel, and it must open clean rather than resurrecting
    // the previous cycle's answer.
    unmount();
    render(<FirstRunFunnel />);

    expect(screen.getByRole("button", { name: /grow my business/i })).toBeInTheDocument();
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
