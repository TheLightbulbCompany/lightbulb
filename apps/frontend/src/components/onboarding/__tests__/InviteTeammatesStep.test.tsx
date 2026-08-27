import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { InviteTeammatesStep } from "../InviteTeammatesStep";

const mockPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: mockPost }),
}));

describe("InviteTeammatesStep", () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  it("posts to /orgs/{org_id}/invitations with email and role", async () => {
    mockPost.mockResolvedValueOnce({ invitation_id: "orginv_1" });
    const onComplete = vi.fn();
    render(<InviteTeammatesStep orgId="org_test" onComplete={onComplete} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "teammate@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        "/orgs/org_test/invitations",
        expect.objectContaining({
          email: "teammate@example.com",
          role: "org:member",
        }),
      );
    });
  });

  it("renders the 409 personal_user_exists message inline", async () => {
    mockPost.mockRejectedValueOnce({
      status: 409,
      body: {
        detail: {
          code: "personal_user_exists",
          message:
            "subscriber@example.com already has an active personal Lightbulb AI subscription. They must cancel it before they can be invited to an organization.",
        },
      },
    });
    render(<InviteTeammatesStep orgId="org_test" onComplete={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "subscriber@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));

    expect(
      await screen.findByText(/already has an active personal Lightbulb AI subscription/i),
    ).toBeInTheDocument();
  });

  it("calls onComplete when 'Done' is clicked", () => {
    const onComplete = vi.fn();
    render(<InviteTeammatesStep orgId="org_test" onComplete={onComplete} />);
    fireEvent.click(screen.getByRole("button", { name: /done/i }));
    expect(onComplete).toHaveBeenCalled();
  });

  it("submits role=org:admin when admin is selected", async () => {
    mockPost.mockResolvedValueOnce({ invitation_id: "orginv_2" });
    render(<InviteTeammatesStep orgId="org_test" onComplete={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "admin@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/role/i), {
      target: { value: "org:admin" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/orgs/org_test/invitations", {
        email: "admin@example.com",
        role: "org:admin",
      });
    });
  });

  it("renders the fallback message when 409 has no body.detail.message", async () => {
    // 401 / 500 / network errors don't carry the structured detail.message
    // shape — verify the component falls back to its own copy instead of
    // crashing on a missing-property render.
    mockPost.mockRejectedValueOnce({ status: 500, body: null });
    render(<InviteTeammatesStep orgId="org_test" onComplete={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "x@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));

    expect(
      await screen.findByText(/Failed to send invitation\. Please try again\./i),
    ).toBeInTheDocument();
  });

  it("surfaces a string detail (Clerk error forwarded by backend)", async () => {
    // clerk_admin.create_organization_invitation raises
    // HTTPException(detail=response.text) on Clerk failures, so duplicate-
    // invite / invalid-role errors arrive as {detail: "<clerk message>"}.
    // Without string-detail handling, org admins would see the generic
    // copy and lose actionable info.
    mockPost.mockRejectedValueOnce({
      status: 422,
      body: { detail: "duplicate invitation: this user is already invited" },
    });
    render(<InviteTeammatesStep orgId="org_test" onComplete={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "dup@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));

    expect(
      await screen.findByText(/duplicate invitation: this user is already invited/i),
    ).toBeInTheDocument();
  });

  it("accumulates multiple sent invites in the list", async () => {
    mockPost
      .mockResolvedValueOnce({ invitation_id: "inv_1" })
      .mockResolvedValueOnce({ invitation_id: "inv_2" });
    render(<InviteTeammatesStep orgId="org_test" onComplete={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "a@b.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));
    await screen.findByText("a@b.com");

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "c@d.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /send invite/i }));
    await screen.findByText("c@d.com");

    expect(screen.getByText("a@b.com")).toBeInTheDocument();
    expect(screen.getByText("c@d.com")).toBeInTheDocument();
  });
});
