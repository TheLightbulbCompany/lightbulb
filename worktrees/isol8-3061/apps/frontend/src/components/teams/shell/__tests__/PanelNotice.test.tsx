// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Wallet } from "lucide-react";

import { PanelNotice, type PanelNoticeState } from "../PanelNotice";

// The tone rule is the load-bearing property of this component and the easiest
// one to erode later: `.interface-design/system.md` says "calm when healthy,
// loud only on incidents". An unonboarded Stripe account, an empty table and a
// not-yet-built tab are all HEALTHY — they are the normal first hour of every
// account. If a future change makes `unbound` amber "so it stands out", every
// new owner sees a working product painted as broken on their first visit.
const HEALTHY: PanelNoticeState[] = [
  "zero",
  "unbound",
  "partial",
  "unavailable",
  "soon",
];

describe("PanelNotice tone", () => {
  it.each(HEALTHY)("renders %s calm — no state colour", (state) => {
    const { container } = render(
      <PanelNotice state={state} headline="Nothing here yet." icon={Wallet} />,
    );
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.className).not.toMatch(/border-l-err|border-l-warn/);
    expect(screen.getByText("Nothing here yet.").className).not.toMatch(/text-err/);
  });

  it("renders locked loud — the one state that is an incident for the owner", () => {
    const { container } = render(
      <PanelNotice state="locked" headline="You're out of credits." icon={Wallet} />,
    );
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.className).toMatch(/border-l-err/);
    expect(screen.getByText("You're out of credits.").className).toMatch(/text-err/);
  });
});

describe("PanelNotice content", () => {
  it("shows the agent prompt in quotes when one is given", () => {
    // The Result pattern: hand the owner the sentence to say to their agent,
    // rather than only telling them what is missing.
    render(
      <PanelNotice
        state="unbound"
        headline="No support widget yet."
        prompt="Add the support chat widget to my app, on every page."
      />,
    );
    expect(
      screen.getByText(/Add the support chat widget to my app, on every page\./),
    ).toBeInTheDocument();
  });

  it("renders the exit as a link when href is given, a button otherwise", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <PanelNotice
        state="locked"
        headline="Out of credits."
        href="/chat?provider=bedrock_claude"
        action={{ label: "Top up now", onClick }}
      />,
    );
    expect(screen.getByRole("link", { name: "Top up now" })).toHaveAttribute(
      "href",
      "/chat?provider=bedrock_claude",
    );

    rerender(
      <PanelNotice
        state="unbound"
        headline="Not connected."
        action={{ label: "Set up payments", onClick }}
      />,
    );
    expect(screen.getByRole("button", { name: "Set up payments" })).toBeInTheDocument();
  });

  it("announces politely — an empty state is not an alert", () => {
    const { container } = render(<PanelNotice state="zero" headline="No runs yet." />);
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.getAttribute("role")).toBe("status");
    expect(notice.getAttribute("aria-live")).toBe("polite");
  });
});
