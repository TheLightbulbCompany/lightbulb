import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeRow, HomeRowsEmpty, HomeSection } from "../HomeSection";
import { handleShellLinkClick } from "@/lib/shellNav";

describe("HomeSection chrome", () => {
  it("names the section, shows the label, and routes the link in-shell while keeping a real href", () => {
    render(
      <HomeSection label="Needs you" href="/chat?panel=teams-inbox" linkLabel="Open Inbox · 3">
        <p>child</p>
      </HomeSection>,
    );
    const section = screen.getByRole("region", { name: "Needs you" });
    expect(within(section).getByText("Needs you")).toBeInTheDocument();
    const link = within(section).getByRole("link", { name: "Open Inbox · 3" });
    expect(link).toHaveAttribute("href", "/chat?panel=teams-inbox");
    fireEvent.click(link);
    expect(handleShellLinkClick).toHaveBeenCalledWith(expect.anything(), "/chat?panel=teams-inbox");
  });

  it("renders a row as one anchor with avatar, title, sub and right slot", () => {
    render(
      <HomeRow who="Friday" title="Hire Pulse as an SDR" sub="Atlas · adds a seat" right="2h ago" href="/chat?panel=teams-inbox" />,
    );
    const row = screen.getByRole("link", { name: /Hire Pulse as an SDR/ });
    expect(row).toHaveAttribute("href", "/chat?panel=teams-inbox");
    expect(within(row).getByText("Atlas · adds a seat")).toBeInTheDocument();
    expect(within(row).getByText("2h ago")).toBeInTheDocument();
    // avatarOnly: the name is accessible via role="img", not rendered as text
    // (a full name span would overflow the row's 28px avatar column).
    expect(within(row).getByRole("img", { name: "Friday" })).toBeInTheDocument();
    expect(within(row).queryByText("Friday")).toBeNull();
  });

  it("renders the empty line as a status by default and as an alert on request", () => {
    const { rerender } = render(<HomeRowsEmpty>Nothing is waiting on you.</HomeRowsEmpty>);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing is waiting on you.");
    rerender(<HomeRowsEmpty role="alert">{"Couldn't read."}</HomeRowsEmpty>);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read.");
  });
});
