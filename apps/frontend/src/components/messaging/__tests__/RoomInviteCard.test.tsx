// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

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
import { RoomInviteCard } from "../RoomInviteCard";

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("RoomInviteCard", () => {
  it("sends one invitation, tagged as from chat, and says where they will land", async () => {
    api.post.mockResolvedValue({ id: "inv_1" });
    render(<RoomInviteCard />);
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: " sam@acme.com " } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send invite" })); });
    expect(api.post).toHaveBeenCalledWith("/orgs/org_1/invitations", { email: "sam@acme.com", source: "chat" });
    expect(screen.getByRole("status")).toHaveTextContent("Invite sent to sam@acme.com. They'll land in this channel.");
  });

  it("keeps the form and says why when the send fails", async () => {
    api.post.mockRejectedValue(new ApiError(409, { detail: "That person is already in this workspace." }));
    render(<RoomInviteCard />);
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "in@acme.com" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send invite" })); });
    expect(screen.getByRole("status")).toHaveTextContent("That person is already in this workspace.");
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
  });

  it("copies the join link from chat", async () => {
    copyJoinLink.mockResolvedValue(undefined);
    render(<RoomInviteCard />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy join link" })); });
    expect(copyJoinLink).toHaveBeenCalledWith("chat");
  });

  it("collapses for good on Just me for now", () => {
    const { unmount } = render(<RoomInviteCard />);
    fireEvent.click(screen.getByRole("button", { name: "Just me for now" }));
    expect(screen.getByText("You can invite people any time from Get started.")).toBeInTheDocument();
    unmount();
    render(<RoomInviteCard />);
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
  });
});
