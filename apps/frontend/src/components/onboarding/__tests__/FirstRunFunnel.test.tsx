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
// Mutable active-organization context, same call-time-deref pattern. The
// funnel is a PER-CONTAINER fact and an org shares one container, so the
// draft is owner-scoped (org id when in org context) — a test switches this
// to prove one workspace's draft can't surface in another.
let organization: { id: string } | null = null;
// Clerk resolves the org context independently of the user; while it is
// unresolved `organization` is undefined and MUST NOT be read as "personal".
let orgLoaded = true;
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { id: "user_1", unsafeMetadata, update },
    isLoaded: true,
  }),
  useOrganization: () => ({ organization, isLoaded: orgLoaded }),
}));

import { FirstRunFunnel } from "@/components/onboarding/FirstRunFunnel";

// Mirrored from FirstRunFunnel.tsx (not exported) — the deploy-skew shim.
const SKEW_RETRY_MS = 3000;
const SKEW_RETRIES = 2;

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
  organization = null;
  orgLoaded = true;
  // The funnel persists an in-progress draft (see the remount tests below),
  // so every test must start from an empty one.
  window.sessionStorage.clear();
});

describe("FirstRunFunnel", () => {
  it("walks grow path: choose -> enter url -> submit -> writes metadata", async () => {
    post.mockResolvedValue({ status: "staged" });
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
    post.mockResolvedValue({ status: "staged" });
    render(<FirstRunFunnel />);

    enterGrowPathAndSubmit();

    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.path).toBe("grow");
    });
  });

  it("posts exactly once — no client-side retry loop", async () => {
    // The endpoint is durable intake now: it records the answer under the
    // owner id and returns, so there is nothing for the client to wait out.
    // The retry loop this replaces gave up after ~16s while provisioning took
    // ~33s, and every first-run user who typed fast enough saw an error for a
    // signup that was working.
    post.mockResolvedValue({ status: "staged" });
    render(<FirstRunFunnel />);
    enterGrowPathAndSubmit();

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("does not write briefSubmitted when the response body is not staged/already_submitted", async () => {
    // Deploy-skew guard: an OLDER backend 202s ("team workspace provisioning")
    // while Paperclip warms, and the api client resolves any 2xx. Dismissing
    // the funnel on that would discard the answer, so the body is still
    // checked — the shim just retries a couple of times first.
    vi.useFakeTimers();
    try {
      post.mockResolvedValue({ status: "provisioning" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SKEW_RETRIES * SKEW_RETRY_MS + 1000);
      });

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(SKEW_RETRIES + 1));
      await vi.waitFor(() =>
        expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
      );
      expect(update).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("rides out an older backend's 409 during a deploy window", async () => {
    // The frontend ships ahead of the ECS backend, and the old one 409s
    // "not_ready" for roughly the first ~30s of every first-run signup. Without
    // the shim every user signing up inside that window sees a red error on a
    // signup that is working — the exact bug this change exists to kill.
    vi.useFakeTimers();
    try {
      post.mockRejectedValueOnce({ status: 409 });
      post.mockResolvedValue({ status: "staged" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(SKEW_RETRY_MS + 500);
      });

      await vi.waitFor(() => expect(update).toHaveBeenCalled());
      expect(screen.queryByText(/couldn.t save that just yet/i)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("submits with no input when the user says they don't know yet", async () => {
    // The absent idea IS the signal: BRIEF.md then tells Friday to look them
    // up and pick one. Sending "" instead would brief her with an empty quote.
    post.mockResolvedValue({ status: "staged" });
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
    post.mockRejectedValue({ status: 500 });
    render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    fireEvent.click(screen.getByRole("button", { name: /don.t know yet/i }));

    await waitFor(() =>
      expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
    );

    post.mockResolvedValue({ status: "staged" });
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(post).toHaveBeenLastCalledWith("/onboarding/brief", {
        path: "new",
        input: undefined,
        detail: undefined,
      });
    });
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
    expect(support.getAttribute("href")).toBe("mailto:support@thelightbulb.company");
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
    post.mockResolvedValue({ status: "staged" });
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

  it("does not leak a draft across a workspace switch", () => {
    // Onboarding is a per-CONTAINER fact and an org shares one container, so
    // the draft belongs to the OWNER, not the signed-in user. Someone who
    // starts a brief in org A and then activates an un-onboarded org B in the
    // same tab must get a clean funnel — otherwise A's text is restored and
    // submitted under B's API context.
    organization = { id: "org_a" };
    const { unmount } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "org-a-secret.com" },
    });
    unmount();

    organization = { id: "org_b" };
    render(<FirstRunFunnel />);

    expect(screen.getByRole("button", { name: /grow my business/i })).toBeInTheDocument();
    expect(screen.queryByDisplayValue("org-a-secret.com")).toBeNull();
  });

  it("does not leak a draft across a workspace switch WITHOUT a remount", () => {
    // Harder variant: Clerk's setActive can flip the active org while this
    // component stays mounted. The key changes mid-life, so without a
    // render-time reset the persist effect would immediately write org A's
    // in-progress text under org B's key — the same leak by another route.
    organization = { id: "org_a" };
    const { rerender } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "org-a-secret.com" },
    });

    organization = { id: "org_b" };
    rerender(<FirstRunFunnel />);

    expect(screen.getByRole("button", { name: /grow my business/i })).toBeInTheDocument();
    expect(screen.queryByDisplayValue("org-a-secret.com")).toBeNull();
    expect(window.sessionStorage.getItem("isol8_first_run_draft_v1:org_b")).toBeNull();
  });

  it("restores the draft for the SAME workspace across a remount", () => {
    organization = { id: "org_a" };
    const { unmount } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "acme.com" },
    });
    unmount();

    render(<FirstRunFunnel />);

    expect(screen.getByPlaceholderText(/yourcompany\.com/i)).toHaveValue("acme.com");
  });

  it("does not strand the funnel busy when the workspace changes mid-submit", async () => {
    // submit() never returns to "idle" on success — it relies on the status
    // poll unmounting the funnel. So if the owner changes while a submission
    // is in flight and the component STAYS mounted, the new workspace's
    // funnel inherits `state: "submitting"` and its Continue button is
    // disabled forever. The onboarding front door must never dead-end.
    let resolvePost: (v: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((r) => { resolvePost = r; }));
    organization = { id: "org_a" };
    const { rerender } = render(<FirstRunFunnel />);
    enterGrowPathAndSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    organization = { id: "org_b" };
    rerender(<FirstRunFunnel />);

    // Org B starts clean: back on the picker, and once it has valid input its
    // Continue button must be usable.
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "org-b.com" },
    });
    expect(screen.getByRole("button", { name: /continue/i })).not.toBeDisabled();

    // Org A's in-flight call landing later must not write org B's metadata.
    resolvePost({ status: "staged" });
    await act(async () => { await Promise.resolve(); });
    expect(update).not.toHaveBeenCalled();
  });

  it("does not read a personal draft while the organization context is unresolved", () => {
    // Seed a PERSONAL draft, then mount with the org hook still loading.
    organization = null;
    orgLoaded = true;
    const { unmount } = render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "personal-secret.com" },
    });
    unmount();

    orgLoaded = false;
    render(<FirstRunFunnel />);

    // Owner unknown → restore nothing. Falling back to user.id here would
    // render personal text inside what may be an org workspace.
    expect(screen.queryByDisplayValue("personal-secret.com")).toBeNull();
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
