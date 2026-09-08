import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InboxPage } from "@/components/teams/inbox/InboxPage";

vi.mock("@/components/teams/inbox/InboxNeedsYou", () => ({
  InboxNeedsYou: () => <section id="inbox-needs-you" aria-label="Needs you" />,
}));
vi.mock("@/components/teams/inbox/InboxHappeningNow", () => ({
  InboxHappeningNow: () => <section id="inbox-happening-now" aria-label="Happening now" />,
}));
vi.mock("@/components/teams/inbox/InboxRecently", () => ({
  InboxRecently: () => <section id="inbox-recently" aria-label="Recently" />,
}));

const props = { companyId: "c1", currentUserId: "u1", agents: [] };

describe("InboxPage", () => {
  it("renders the three bands in order and no task list", () => {
    render(<InboxPage {...props} />);
    const regions = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(regions).toEqual(["Needs you", "Happening now", "Recently"]);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByPlaceholderText(/search/i)).toBeNull();
  });

  it("scrolls to the requested section, scoped to its own root", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { container } = render(<InboxPage {...props} section="recently" />);
    expect(scrollIntoView).toHaveBeenCalled();
    expect(container.querySelector("#inbox-recently")).not.toBeNull();
  });

  it("does not scroll when no section was asked for", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<InboxPage {...props} />);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
