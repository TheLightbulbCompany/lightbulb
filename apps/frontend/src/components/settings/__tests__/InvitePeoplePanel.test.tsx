// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useApi: () => api,
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: "org_1" }) }));
const copyJoinLink = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useCopyJoinLink", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useCopyJoinLink: () => copyJoinLink,
}));

import { ApiError } from "@/lib/api";
import { InvitePeoplePanel } from "../InvitePeoplePanel";

const row = (id: string, email: string) => ({ id, email, created_at: new Date(Date.now() - 2 * 3600_000).toISOString(), expires_at: null, status: "pending" as const });

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ invitations: [row("inv_a", "ann@acme.com"), row("inv_b", "bob@acme.com")] });
});

describe("InvitePeoplePanel", () => {
  it("sends each address and shows one result line per address", async () => {
    api.get.mockResolvedValue({ invitations: [] });
    api.post.mockImplementation(async (_path: string, { email }: { email: string }) => {
      if (email === "in@acme.com") throw new ApiError(409, { detail: "That person is already in this workspace." });
      if (email === "dup@acme.com") throw new ApiError(409, { detail: "That email already has a pending invitation." });
      if (email === "nope") throw new ApiError(422, { detail: "invalid" });
      return { ...row(`inv_${email}`, email), created_at: new Date().toISOString() };
    });
    render(<InvitePeoplePanel source="test" />);
    fireEvent.change(await screen.findByLabelText("Email address"), { target: { value: "new@acme.com, in@acme.com  dup@acme.com nope" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send invites" })); });
    expect(api.post).toHaveBeenCalledTimes(4);
    expect(api.post).toHaveBeenCalledWith("/orgs/org_1/invitations", { email: "new@acme.com", source: "test" });
    const lines = within(screen.getByRole("list", { name: "Invite results" })).getAllByRole("listitem").map((li) => li.textContent);
    expect(lines).toEqual([
      "new@acme.com: Invitation sent.",
      "in@acme.com: That person is already in this workspace.",
      "dup@acme.com: That email already has a pending invitation.",
      "nope: That doesn't look like an email address.",
    ]);
    // The sent one joins the pending list; what failed stays in the field to fix.
    expect(screen.getByText("Sent just now")).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveValue("in@acme.com, dup@acme.com, nope");
  });

  it("lists pending invitations and revokes one optimistically", async () => {
    api.del.mockResolvedValue(undefined);
    render(<InvitePeoplePanel source="test" />);
    expect(await screen.findByText("ann@acme.com")).toBeInTheDocument();
    expect(screen.getAllByText("Sent 2h ago")).toHaveLength(2);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Revoke invitation to ann@acme.com" })); });
    expect(api.del).toHaveBeenCalledWith("/orgs/org_1/invitations/inv_a");
    expect(screen.queryByText("ann@acme.com")).not.toBeInTheDocument();
  });

  it("puts a revoked row back in place when the revoke fails", async () => {
    let fail!: (e: unknown) => void;
    api.del.mockReturnValue(new Promise((_r, reject) => { fail = reject; }));
    render(<InvitePeoplePanel source="test" />);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke invitation to ann@acme.com" }));
    expect(screen.queryByText("ann@acme.com")).not.toBeInTheDocument();
    await act(async () => { fail(new ApiError(500, {})); });
    const emails = screen.getAllByRole("listitem").map((li) => li.querySelector("span")?.textContent);
    expect(emails).toEqual(["ann@acme.com", "bob@acme.com"]);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't revoke the invitation to ann@acme.com.");
  });

  it("lets a non-admin member invite and copy the link, without the admin-only pending list", async () => {
    api.get.mockRejectedValue(new ApiError(403, { detail: "admin only" }));
    render(<InvitePeoplePanel source="test" />);
    expect(await screen.findByLabelText("Email address")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send invites" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy join link" })).toBeInTheDocument();
    expect(screen.queryByText("Pending invitations")).not.toBeInTheDocument();
  });

  it("copies the join link and says so inline", async () => {
    copyJoinLink.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new ApiError(403, {}));
    render(<InvitePeoplePanel source="invite_dialog" />);
    const copy = await screen.findByRole("button", { name: "Copy join link" });
    await act(async () => { fireEvent.click(copy); });
    expect(copyJoinLink).toHaveBeenCalledWith("invite_dialog");
    expect(screen.getByRole("status")).toHaveTextContent("Copied. Anyone who opens it and signs in joins this workspace.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy join link" })); });
    expect(screen.getByRole("status")).toHaveTextContent("Ask a workspace admin for a new one.");
  });
});
