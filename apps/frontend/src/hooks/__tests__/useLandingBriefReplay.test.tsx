import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

const post = vi.fn();
// A single stable object, not a fresh literal per call — `useApi()` is
// `useMemo`'d in production (src/lib/api.ts), so the hook's effect (which
// depends on `api`) must not spuriously re-fire on every render here.
const apiMock = { post };
vi.mock("@/lib/api", () => ({ useApi: () => apiMock }));

// Mutable active-organization context, read at useOrganization() call time —
// same pattern as the FirstRunFunnel tests. `orgLoaded: false` means the
// owner is UNKNOWN and the stash must not be routed anywhere yet.
let organization: { id: string } | null = null;
let orgLoaded = true;
let user: { id: string } | null = { id: "user_1" };
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization, isLoaded: orgLoaded }),
  useUser: () => ({ user }),
}));

const writeFirstRun = vi.fn((_u: unknown, _p: unknown) => Promise.resolve());
vi.mock("@/lib/firstRun", () => ({
  writeFirstRun: (u: unknown, p: unknown) => writeFirstRun(u, p),
}));

import {
  LANDING_BRIEF_STORAGE_KEY,
  useLandingBriefReplay,
} from "@/hooks/useLandingBriefReplay";
import type { ProvisioningPhase } from "@/hooks/useProvisioningState";

interface Props {
  gatePhase: ProvisioningPhase;
  blockedCode?: string | null;
  awaitingBrief: boolean;
  isTerminalError: boolean;
}

// The exact state in which the in-chat funnel would render.
const READY: Props = { gatePhase: "normal", awaitingBrief: true, isTerminalError: false };

function stash(brief: unknown) {
  window.sessionStorage.setItem(
    LANDING_BRIEF_STORAGE_KEY,
    typeof brief === "string" ? brief : JSON.stringify(brief),
  );
}

function mount(initialProps: Props = READY) {
  return renderHook((p: Props) => useLandingBriefReplay(p), { initialProps });
}

beforeEach(() => {
  post.mockReset();
  writeFirstRun.mockClear();
  organization = null;
  orgLoaded = true;
  user = { id: "user_1" };
  window.sessionStorage.clear();
});

describe("useLandingBriefReplay", () => {
  it("submits the stash once, deleting it at read time", async () => {
    let resolvePost: (v: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((r) => { resolvePost = r; }));
    stash({ path: "grow", input: "acme.com", detail: "" });

    const { result, rerender } = mount();

    // Deleted the moment it is read — BEFORE the POST resolves — so a
    // remount can never fire a second automatic submit.
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
    expect(post).toHaveBeenCalledWith("/onboarding/brief", {
      path: "grow",
      input: "acme.com",
      detail: undefined,
    });
    // Funnel suppressed while the POST is in flight...
    expect(result.current.replaying).toBe(true);

    await act(async () => { resolvePost({ status: "staged" }); });

    // ...and after success: the next status poll flips awaitingBrief, not us.
    expect(result.current.replaying).toBe(true);
    rerender(READY);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("a pre-drop held user still stashes the idea; any other blocked code does not", async () => {
    post.mockResolvedValue({ status: "staged" });
    stash({ path: "new", input: "A bakery for dogs", detail: "" });

    // Every other blocked code is a dead end for a brief.
    const { unmount } = mount({ ...READY, gatePhase: "blocked", blockedCode: "trial_expired" });
    expect(post).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).not.toBeNull();
    unmount();

    // launch_pending is the one blocked state that still takes the brief —
    // POST /onboarding/brief is intake only and needs no container.
    mount({ ...READY, gatePhase: "blocked", blockedCode: "launch_pending" });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/onboarding/brief", {
      path: "new",
      input: "A bakery for dogs",
      detail: undefined,
    });
  });

  it("holds fire until every condition is true", async () => {
    post.mockResolvedValue({ status: "staged" });
    stash({ path: "new", input: "A bakery for dogs", detail: "Dog owners" });

    const { rerender } = mount({ ...READY, gatePhase: "loading" });
    expect(post).not.toHaveBeenCalled();

    rerender({ ...READY, awaitingBrief: false });
    rerender({ ...READY, isTerminalError: true });
    expect(post).not.toHaveBeenCalled();

    // Owner unknown (org context unresolved) also holds fire — an org stash
    // must not be auto-POSTed as "personal".
    orgLoaded = false;
    rerender(READY);
    expect(post).not.toHaveBeenCalled();
    // Untouched the whole time: no condition was met, so no read happened.
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).not.toBeNull();

    orgLoaded = true;
    rerender(READY);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/onboarding/brief", {
      path: "new",
      input: "A bakery for dogs",
      detail: "Dog owners",
    });
  });

  it("posts the empty new-path idea as absent — the you-pick flow", async () => {
    post.mockResolvedValue({ status: "staged" });
    stash({ path: "new", input: "", detail: "" });

    mount();

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/onboarding/brief", {
        path: "new",
        input: undefined,
        detail: undefined,
      }),
    );
  });

  it("org context copies the stash into the funnel's draft key and never POSTs", () => {
    organization = { id: "org_1" };
    stash({ path: "grow", input: "acme.com", detail: "" });

    const { result } = mount();

    expect(post).not.toHaveBeenCalled();
    expect(result.current.replaying).toBe(false);
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
    expect(
      JSON.parse(window.sessionStorage.getItem("isol8_first_run_draft_v1:org_1")!),
    ).toEqual({ path: "grow", input: "acme.com", detail: "" });
  });

  it("drops an invalid stash silently", () => {
    const invalid = [
      "{not json",
      { path: "bogus", input: "hello there" },
      { path: "grow", input: "ab" }, // present-but-tiny 422s server-side
      { path: "grow", input: "" }, // grow requires the website
    ];
    for (const bad of invalid) {
      stash(bad);
      const { result, unmount } = mount();
      expect(post).not.toHaveBeenCalled();
      expect(result.current.replaying).toBe(false);
      // Still consumed: a bad stash must not be retried forever.
      expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
      unmount();
    }
  });

  it("treats already_submitted as success", async () => {
    post.mockResolvedValue({ status: "already_submitted" });
    stash({ path: "grow", input: "acme.com", detail: "" });

    const { result } = mount();
    await act(async () => {});

    expect(result.current.replaying).toBe(true);
  });

  it("hands back to the funnel when the POST fails, restoring the stash for a retry", async () => {
    post.mockRejectedValue({ status: 500 });
    const brief = { path: "grow", input: "acme.com", detail: "" };
    stash(brief);

    const { result } = mount();
    await act(async () => {});

    expect(result.current.replaying).toBe(false);
    expect(post).toHaveBeenCalledTimes(1);
    // Held users now sit on this path for days before the drop — losing the
    // brief on a network blip is not acceptable, so it's put back.
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBe(JSON.stringify(brief));
  });

  it("restores the stash and retries when the resolved response has an unexpected status", async () => {
    post.mockResolvedValueOnce({ status: "weird" });
    const brief = { path: "grow", input: "acme.com", detail: "" };
    stash(brief);

    const { result, rerender } = mount();
    await act(async () => {});

    // Not a success — must not be treated like "staged"/"already_submitted".
    expect(result.current.replaying).toBe(false);
    expect(post).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBe(JSON.stringify(brief));

    post.mockResolvedValue({ status: "staged" });
    // A gate-condition change re-fires the effect (firedRef was reset).
    rerender({ ...READY, awaitingBrief: false });
    rerender(READY);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
  });

  it("a later render POSTs again after a failed attempt restored the stash", async () => {
    post.mockRejectedValueOnce({ status: 500 });
    stash({ path: "grow", input: "acme.com", detail: "" });

    const { rerender } = mount();
    await act(async () => {});
    expect(post).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).not.toBeNull();

    post.mockResolvedValue({ status: "staged" });
    // A gate-condition change re-fires the effect (firedRef was reset on failure).
    rerender({ ...READY, awaitingBrief: false });
    rerender(READY);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
  });

  it("does not clobber a newer stash written while the failed POST was restoring", async () => {
    stash({ path: "grow", input: "acme.com", detail: "" });

    // Simulate a fresh landing-hero write landing in the gap between the
    // stash being read (and deleted) and the failure restore running.
    const newer = JSON.stringify({ path: "new", input: "a newer idea", detail: "" });
    post.mockImplementation(async () => {
      window.sessionStorage.setItem(LANDING_BRIEF_STORAGE_KEY, newer);
      throw { status: 500 };
    });

    mount();
    await act(async () => {});

    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBe(newer);
  });

  it("drops a stash whose JSON is valid but not an object", async () => {
    // "null" parses fine; reading .path off it must not throw in the effect.
    stash("null");

    const { result } = mount();
    await act(async () => {});

    expect(post).not.toHaveBeenCalled();
    expect(result.current.replaying).toBe(false);
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).toBeNull();
  });

  it("records first-run metadata after a successful replay", async () => {
    post.mockResolvedValue({ status: "staged" });
    stash({ path: "new", input: "a ceramics studio", detail: "" });

    mount();
    await waitFor(() => expect(writeFirstRun).toHaveBeenCalledTimes(1));

    expect(writeFirstRun).toHaveBeenCalledWith(
      { id: "user_1" },
      { briefSubmitted: true, path: "new" },
    );
  });

  it("bumps orgDraftVersion so the consumer remounts the funnel", async () => {
    organization = { id: "org_9" };
    stash({ path: "grow", input: "acme.com", detail: "" });

    const { result } = mount();
    await act(async () => {});

    // The funnel mounted before the copy and reads its draft only at mount —
    // the version bump is what triggers the keyed remount.
    expect(result.current.orgDraftVersion).toBe(1);
    expect(post).not.toHaveBeenCalled();
    expect(writeFirstRun).not.toHaveBeenCalled();
  });

  it("never replaces an existing org draft with a stale landing stash", async () => {
    organization = { id: "org_9" };
    const draftKey = "isol8_first_run_draft_v1:org_9";
    const deliberate = JSON.stringify({ path: "new", input: "the real company brief", detail: "" });
    window.sessionStorage.setItem(draftKey, deliberate);
    stash({ path: "grow", input: "stale.example.com", detail: "" });

    const { result } = mount();
    await act(async () => {});

    // Deliberate company-scoped intent wins; no remount is forced either.
    expect(window.sessionStorage.getItem(draftKey)).toBe(deliberate);
    expect(result.current.orgDraftVersion).toBe(0);
    expect(post).not.toHaveBeenCalled();
  });

  it("leaves the stash untouched until the Clerk user resolves (personal path)", async () => {
    user = null;
    stash({ path: "new", input: "a ceramics studio", detail: "" });

    const { rerender } = mount();
    await act(async () => {});

    // Not consumed, not posted — the metadata mirror needs the user, so the
    // stash must survive the transient null.
    expect(post).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY)).not.toBeNull();

    user = { id: "user_1" };
    post.mockResolvedValue({ status: "staged" });
    rerender(READY);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(writeFirstRun).toHaveBeenCalledTimes(1));
  });
});
