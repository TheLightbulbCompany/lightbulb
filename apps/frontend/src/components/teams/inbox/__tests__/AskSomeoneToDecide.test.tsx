import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const h = vi.hoisted(() => ({ post: vi.fn(), membersCount: 1 }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ organization: { id: "org_1", membersCount: h.membersCount } }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useApi: () => ({ post: h.post }),
}));

import { AskSomeoneToDecide } from "../AskSomeoneToDecide";

beforeEach(() => { h.post.mockReset(); h.membersCount = 1; });

describe("AskSomeoneToDecide", () => {
  it("invites someone to this decision from a one-person workspace", async () => {
    h.post.mockResolvedValue({ id: "inv_1" });
    render(<AskSomeoneToDecide approvalId="ap-1" decision="Start outreach?" />);
    fireEvent.click(screen.getByRole("button", { name: "Ask someone else to decide" }));
    fireEvent.change(screen.getByLabelText("Their email"), { target: { value: "sam@acme.com" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send invite" })); });
    expect(h.post).toHaveBeenCalledWith("/orgs/org_1/invitations", {
      email: "sam@acme.com", source: "approval", approval_id: "ap-1", decision: "Start outreach?",
    });
    expect(screen.getByRole("status")).toHaveTextContent("Asked sam@acme.com.");
  });

  it("is absent once the workspace has a second person", () => {
    h.membersCount = 2;
    const { container } = render(<AskSomeoneToDecide approvalId="ap-1" decision="x" />);
    expect(container).toBeEmptyDOMElement();
  });
});
