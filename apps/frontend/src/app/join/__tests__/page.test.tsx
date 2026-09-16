import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// The join page: /join/{orgId}#{token}. The invariants worth pinning are that
// the token is scrubbed from the URL and travels in the POST body, that a
// rejected link says so without offering a retry, and that already-a-member is
// a success.

const post = vi.hoisted(() => vi.fn());
const apiClient = vi.hoisted(() => ({}) as Record<string, unknown>);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  Object.assign(apiClient, { post });
  return { ...actual, useApi: () => apiClient };
});

const nav = vi.hoisted(() => ({
  params: { orgId: "org_1" } as Record<string, string>,
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useParams: () => nav.params,
  useRouter: () => ({ push: nav.push }),
}));

const clerk = vi.hoisted(() => ({
  isLoaded: true,
  isSignedIn: true,
  orgListLoaded: true,
  setActive: vi.fn().mockResolvedValue(undefined),
  signInButtonProps: null as Record<string, unknown> | null,
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.isLoaded, isSignedIn: clerk.isSignedIn }),
  useOrganizationList: () => ({ isLoaded: clerk.orgListLoaded, setActive: clerk.setActive }),
  // Keep the props: dropping them is how a missing redirect shipped untested.
  SignInButton: ({ children, ...props }: { children: React.ReactNode }) => {
    clerk.signInButtonProps = props;
    return children;
  },
}));

import { ApiError } from "@/lib/api";
import JoinWorkspacePage, { joinErrorMessage } from "../[orgId]/page";

describe("JoinWorkspacePage", () => {
  beforeEach(() => {
    post.mockReset();
    nav.push.mockReset();
    clerk.setActive.mockReset().mockResolvedValue(undefined);
    clerk.isLoaded = true;
    clerk.isSignedIn = true;
    clerk.orgListLoaded = true;
    clerk.signInButtonProps = null;
    nav.params = { orgId: "org_1" };
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/join/org_1#s3cr3t-token");
    post.mockResolvedValue({ status: "joined", org_id: "org_1", org_name: "Acme" });
  });

  it("redeems with the token in the BODY, then activates the workspace", async () => {
    render(<JoinWorkspacePage />);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/orgs/org_1/join", { token: "s3cr3t-token" }),
    );
    expect(window.location.hash).toBe("");
    // The credential must never ride in the URL to the API host.
    expect(post.mock.calls[0][0]).not.toContain("s3cr3t-token");

    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_1" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/chat"));
    expect(await screen.findByText(/welcome to acme/i)).toBeInTheDocument();
  });

  it("treats already-a-member as a success", async () => {
    post.mockResolvedValue({ status: "already_member", org_id: "org_1", org_name: "Acme" });
    render(<JoinWorkspacePage />);

    expect(await screen.findByText(/already in acme/i)).toBeInTheDocument();
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/chat"));
  });

  it("shows the backend's sentence for a dead link, with no retry", async () => {
    post.mockRejectedValue(
      new ApiError(404, { detail: "This join link is no longer valid." }, "boom"),
    );
    render(<JoinWorkspacePage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/no longer valid/i);
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
    expect(nav.push).not.toHaveBeenCalled();
    // Dropped at once, so the onboarding gate can't send the tab back to a dead link.
    expect(window.sessionStorage.getItem("lightbulb:workspace-join:org_1")).toBeNull();
  });

  it("offers a retry for a transient failure", async () => {
    post.mockRejectedValueOnce(new ApiError(502, { detail: "nope" }, "boom"));
    const user = userEvent.setup();
    render(<JoinWorkspacePage />);

    const retry = await screen.findByRole("button", { name: /try again/i });
    post.mockResolvedValue({ status: "joined", org_id: "org_1", org_name: "Acme" });
    await user.click(retry);

    expect(await screen.findByText(/welcome to acme/i)).toBeInTheDocument();
  });

  it("keeps a retryable token until the holder leaves the page", async () => {
    post.mockRejectedValue(new ApiError(502, { detail: "nope" }, "boom"));
    const { unmount } = render(<JoinWorkspacePage />);

    await screen.findByRole("button", { name: /try again/i });
    expect(window.sessionStorage.getItem("lightbulb:workspace-join:org_1")).toBe("s3cr3t-token");
    unmount();
    expect(window.sessionStorage.getItem("lightbulb:workspace-join:org_1")).toBeNull();
  });

  it("falls back to /onboarding when Clerk cannot activate the new membership yet", async () => {
    clerk.setActive.mockRejectedValue(new Error("not ready"));
    render(<JoinWorkspacePage />);
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/onboarding"));
  });

  it("does not redeem before Clerk has resolved the session", () => {
    clerk.isLoaded = false;
    render(<JoinWorkspacePage />);
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps the token in session storage while a signed-out holder authenticates", async () => {
    clerk.isSignedIn = false;
    render(<JoinWorkspacePage />);

    expect(await screen.findByRole("button", { name: /sign in to join/i })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(window.sessionStorage.getItem("lightbulb:workspace-join:org_1")).toBe("s3cr3t-token");
    expect(post).not.toHaveBeenCalled();
  });

  it("sends every sign-in and sign-up, OAuth included, back to this page without the token", async () => {
    clerk.isSignedIn = false;
    render(<JoinWorkspacePage />);

    await screen.findByRole("button", { name: /sign in to join/i });
    expect(clerk.signInButtonProps).toEqual({
      mode: "modal",
      forceRedirectUrl: "/join/org_1",
      signUpForceRedirectUrl: "/join/org_1",
    });
  });

  it("waits for the organization switcher before redeeming", () => {
    clerk.orgListLoaded = false;
    render(<JoinWorkspacePage />);
    expect(post).not.toHaveBeenCalled();
  });
});

describe("joinErrorMessage", () => {
  it("marks a 404 as terminal", () => {
    expect(joinErrorMessage(new ApiError(404, { detail: "gone" })).canRetry).toBe(false);
  });

  it("shows a full workspace's 409 sentence and does not offer retry", () => {
    const r = joinErrorMessage(new ApiError(409, { detail: "This workspace has reached its member limit." }));
    expect(r.message).toMatch(/member limit/);
    expect(r.canRetry).toBe(false);
  });

  it("marks a 5xx as retryable", () => {
    expect(joinErrorMessage(new ApiError(503, {})).canRetry).toBe(true);
  });

  it("tells a signed-out-looking caller to sign in again", () => {
    expect(joinErrorMessage(new ApiError(401, {})).message).toMatch(/sign in again/i);
  });
});
