import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

// The workspace members panel: the roster (GET /teams/members, the endpoint the
// chat workspace already uses) plus the one shareable join link. Replaces the
// email-invite form's tests — there is no email field and no role select any
// more, and the assertions below pin that down as behaviour, not an omission.

const get = vi.hoisted(() => vi.fn());
const post = vi.hoisted(() => vi.fn());
const del = vi.hoisted(() => vi.fn());

// importOriginal keeps the REAL ApiError class, so the component's
// `err instanceof ApiError` branch is exercised rather than mocked away.
// One STABLE object, as the real `useApi` returns (it is useMemo'd on the
// Clerk token getter). A fresh literal per render would re-fire every
// `useEffect` that lists `api` as a dep, which is not how the app behaves.
const apiClient = vi.hoisted(() => ({}) as Record<string, unknown>);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  Object.assign(apiClient, { get, post, del });
  return { ...actual, useApi: () => apiClient };
});

const teams = vi.hoisted(() => ({
  read: vi.fn(),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: teams.read }),
}));

const clerk = vi.hoisted(() => ({
  isLoaded: true,
  organization: null as { id: string; name: string } | null,
  membership: null as { role: string } | null,
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("jwt"), isLoaded: true }),
  useOrganization: () => ({
    organization: clerk.organization,
    membership: clerk.membership,
    isLoaded: clerk.isLoaded,
  }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";
import { ApiError } from "@/lib/api";
import { WorkspaceMembersSection, joinLinkErrorMessage } from "../WorkspaceMembersSection";

function asAdmin() {
  clerk.isLoaded = true;
  clerk.organization = { id: "org_1", name: "Acme" };
  clerk.membership = { role: "org:admin" };
}

function withMembers(members: unknown[]) {
  teams.read.mockReturnValue({ data: { members }, error: undefined, isLoading: false });
}

describe("WorkspaceMembersSection", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    del.mockReset();
    teams.read.mockReset();
    get.mockResolvedValue({ exists: false });
    post.mockResolvedValue({
      join_url: "https://dev.thelightbulb.company/join/org_1/s3cr3t-token",
      created_at: "2026-09-14T00:00:00+00:00",
    });
    del.mockResolvedValue(undefined);
    withMembers([]);
    clerk.isLoaded = true;
    clerk.organization = null;
    clerk.membership = null;
  });

  // --- The member list ----------------------------------------------------

  it("renders the workspace roster from GET /teams/members", async () => {
    asAdmin();
    withMembers([
      { userId: "user_1", name: "Ada Lovelace", email: "ada@example.com" },
      { userId: "user_2", name: "", email: "grace@example.com" },
    ]);
    render(<WorkspaceMembersSection />);

    const roster = await screen.findByTestId("members-roster");
    expect(within(roster).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(roster).getByText("ada@example.com")).toBeInTheDocument();
    // No display name -> the email is the label, not the raw Clerk id.
    expect(within(roster).getByText("grace@example.com")).toBeInTheDocument();
    expect(within(roster).queryByText("user_2")).not.toBeInTheDocument();

    // It must be THAT endpoint — this component does not get its own.
    expect(teams.read).toHaveBeenCalledWith("/members");
  });

  it("says so plainly when the roster is empty", async () => {
    asAdmin();
    withMembers([]);
    render(<WorkspaceMembersSection />);
    expect(await screen.findByText(/nobody here yet/i)).toBeInTheDocument();
  });

  it("degrades to a retry line when the Teams BFF is still provisioning", async () => {
    asAdmin();
    teams.read.mockReturnValue({ data: undefined, error: new Error("202"), isLoading: false });
    render(<WorkspaceMembersSection />);
    expect(await screen.findByText(/still coming up/i)).toBeInTheDocument();
  });

  // --- The join link ------------------------------------------------------

  it("mints the first link and copies it", async () => {
    asAdmin();
    const user = userEvent.setup();
    // userEvent.setup() installs its own navigator.clipboard stub, so the spy
    // has to go on AFTER it or it is silently replaced.
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<WorkspaceMembersSection />);

    await user.click(await screen.findByRole("button", { name: /create join link/i }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/orgs/org_1/join-link", {}));
    const field = await screen.findByLabelText(/your join link/i);
    expect(field).toHaveValue("https://dev.thelightbulb.company/join/org_1/s3cr3t-token");

    await user.click(screen.getByRole("button", { name: /copy link/i }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("https://dev.thelightbulb.company/join/org_1/s3cr3t-token"),
    );
    expect(await screen.findByRole("button", { name: /copied/i })).toBeInTheDocument();
  });

  it("reports join_link_copied from settings when the link is copied", async () => {
    asAdmin();
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<WorkspaceMembersSection />);
    await user.click(await screen.findByRole("button", { name: /create join link/i }));
    await user.click(await screen.findByRole("button", { name: /copy link/i }));
    await waitFor(() => expect(capture).toHaveBeenCalledWith("join_link_copied", { source: "members_settings", workspace_id: "org_1" }));
  });

  it("shows the current link from GET so re-sharing never needs a regenerate", async () => {
    asAdmin();
    get.mockResolvedValue({
      exists: true,
      join_url: "https://dev.thelightbulb.company/join/org_1#curr3nt",
      created_at: "2026-09-01T00:00:00+00:00",
      created_by: "user_admin",
    });
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<WorkspaceMembersSection />);

    const field = await screen.findByLabelText(/your join link/i);
    expect(field).toHaveValue("https://dev.thelightbulb.company/join/org_1#curr3nt");
    await user.click(screen.getByRole("button", { name: /copy link/i }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("https://dev.thelightbulb.company/join/org_1#curr3nt"),
    );
    // Nothing was minted: showing the link must not rotate it.
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /regenerate link/i })).toBeInTheDocument();
  });

  it("regenerating replaces the shown link with the new one", async () => {
    asAdmin();
    get.mockResolvedValue({
      exists: true,
      join_url: "https://dev.thelightbulb.company/join/org_1#curr3nt",
      created_at: "2026-09-01T00:00:00+00:00",
    });
    const user = userEvent.setup();
    render(<WorkspaceMembersSection />);

    await screen.findByLabelText(/your join link/i);
    await user.click(screen.getByRole("button", { name: /regenerate link/i }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/orgs/org_1/join-link", {}));
    expect(await screen.findByLabelText(/your join link/i)).toHaveValue(
      "https://dev.thelightbulb.company/join/org_1/s3cr3t-token",
    );
  });

  it("offers a one-time regenerate for a link minted before links could be shown", async () => {
    asAdmin();
    get.mockResolvedValue({
      exists: true,
      created_at: "2026-09-01T00:00:00+00:00",
      created_by: "user_admin",
    });
    render(<WorkspaceMembersSection />);

    expect(await screen.findByTestId("join-link-active")).toHaveTextContent(/regenerate/i);
    expect(screen.queryByLabelText(/your join link/i)).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /regenerate link/i })).toBeInTheDocument();
  });

  it("revokes the link and drops back to the create state", async () => {
    asAdmin();
    get.mockResolvedValue({ exists: true, created_at: "2026-09-01T00:00:00+00:00" });
    const user = userEvent.setup();
    render(<WorkspaceMembersSection />);

    await user.click(await screen.findByRole("button", { name: /turn off link/i }));

    await waitFor(() => expect(del).toHaveBeenCalledWith("/orgs/org_1/join-link"));
    expect(await screen.findByTestId("join-link-none")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /turn off link/i })).not.toBeInTheDocument();
  });

  it("has no email field and no role select — the model the owner rejected", async () => {
    asAdmin();
    render(<WorkspaceMembersSection />);
    await screen.findByTestId("members-join-link");

    expect(screen.queryByLabelText(/email address/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^role$/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  // --- Gating -------------------------------------------------------------

  it("hides the roster and the link from a non-admin org member", async () => {
    clerk.organization = { id: "org_1", name: "Acme" };
    clerk.membership = { role: "org:member" };
    render(<WorkspaceMembersSection />);

    expect(screen.getByTestId("members-restricted")).toBeInTheDocument();
    expect(screen.queryByTestId("members-join-link")).not.toBeInTheDocument();
    expect(screen.queryByTestId("members-roster")).not.toBeInTheDocument();
    // A member must not be able to mint a credential — not even by request.
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    // …and no roster read goes out either (SWR's null-key conditional fetch).
    expect(teams.read).toHaveBeenCalledWith(null);
  });

  it("offers workspace creation instead of a link on a personal workspace", () => {
    clerk.organization = null;
    clerk.membership = null;
    render(<WorkspaceMembersSection />);

    expect(screen.getByTestId("members-personal")).toBeInTheDocument();
    expect(screen.queryByTestId("members-join-link")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /create a workspace/i })).toHaveAttribute(
      "href",
      "/onboarding",
    );
  });

  it("renders nothing decisive while Clerk is still resolving the org", () => {
    // An unset organization during the load window must not read as personal —
    // that would flash the create-a-workspace card at an org admin.
    clerk.isLoaded = false;
    clerk.organization = null;
    render(<WorkspaceMembersSection />);

    expect(screen.queryByTestId("members-personal")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: /loading workspace/i })).toBeInTheDocument();
  });

  // --- Errors -------------------------------------------------------------

  it("surfaces a mint failure and stays usable", async () => {
    asAdmin();
    post.mockRejectedValue(new ApiError(503, { detail: "Clerk API unavailable" }, "boom"));
    const user = userEvent.setup();
    render(<WorkspaceMembersSection />);

    await user.click(await screen.findByRole("button", { name: /create join link/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.queryByLabelText(/your join link/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create join link/i })).toBeEnabled();
  });
});

describe("joinLinkErrorMessage", () => {
  it("maps a 403 to admin-only copy", () => {
    expect(joinLinkErrorMessage(new ApiError(403, { detail: "Cannot manage a different workspace" }))).toMatch(
      /only a workspace admin/i,
    );
  });

  it("maps a 503 to a retry message", () => {
    expect(joinLinkErrorMessage(new ApiError(503, { detail: "Clerk API unavailable" }))).toMatch(
      /unavailable/i,
    );
  });

  it("passes a plain backend sentence through", () => {
    expect(joinLinkErrorMessage(new ApiError(500, { detail: "Internal Server Error" }))).toBe(
      "Internal Server Error",
    );
  });

  it("falls back to generic copy for a non-API failure", () => {
    expect(joinLinkErrorMessage(new TypeError("network down"))).toMatch(/something went wrong/i);
  });
});
