// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";

import { ChatErrorCard } from "@/components/chat/ChatErrorCard";

afterEach(() => vi.restoreAllMocks());

describe("ChatErrorCard action link", () => {
  it("navigates via pushState on plain left-click (no router round-trip)", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="rate_limit" message="" />);
    const link = screen.getByRole("link", { name: /switch provider/i });
    expect(link).toHaveAttribute("href", "/chat?panel=llm&action=change-provider");
    fireEvent.click(link, { button: 0 });
    expect(push).toHaveBeenCalledWith(null, "", "/chat?panel=llm&action=change-provider");
  });

  it("lets modified clicks fall through to native anchor behavior", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="rate_limit" message="" />);
    fireEvent.click(screen.getByRole("link", { name: /switch provider/i }), {
      button: 0,
      metaKey: true,
    });
    expect(push).not.toHaveBeenCalled();
  });

  it("navigates cross-route CTAs with a real Link, not pushState", () => {
    // /settings is a DIFFERENT Next route. pushState would change the URL bar
    // while leaving the chat shell mounted — the upgrade CTA would silently do
    // nothing. Only in-shell ?panel swaps may use pushState.
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="trial_daily_cap" message="" />);
    const link = screen.getByRole("link", { name: "Upgrade to keep going" });
    expect(link).toHaveAttribute("href", "/settings?panel=billing");
    fireEvent.click(link, { button: 0 });
    expect(push).not.toHaveBeenCalled();
  });

  it("keeps pushState for in-shell CTAs", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="out_of_credits" message="" />);
    fireEvent.click(screen.getByRole("link", { name: "Top up now" }), { button: 0 });
    expect(push).toHaveBeenCalledWith(null, "", "/chat?provider=bedrock_claude");
  });
});
