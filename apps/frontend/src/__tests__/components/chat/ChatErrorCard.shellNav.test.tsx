// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";

import { ChatErrorCard } from "@/components/chat/ChatErrorCard";

// A Workspace plan with no provider chosen: included_daily_cap's in-shell
// "Pick provider" CTA.
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { plan: "standard", provider_choice: null, is_subscribed: true } }),
}));

afterEach(() => vi.restoreAllMocks());

describe("ChatErrorCard action link", () => {
  it("lets modified clicks fall through to native anchor behavior", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="included_daily_cap" message="" />);
    fireEvent.click(screen.getByRole("link", { name: /pick provider/i }), {
      button: 0,
      metaKey: true,
    });
    expect(push).not.toHaveBeenCalled();
  });

  it("navigates cross-route CTAs with a real Link, not pushState", () => {
    // /settings is a DIFFERENT Next route. pushState would change the URL bar
    // while leaving the chat shell mounted — the CTA would silently do
    // nothing. Only in-shell ?panel swaps may use pushState.
    //
    // Uses subscription_inactive rather than trial_daily_cap: the two trial
    // codes no longer link anywhere, they render the provider picker inline
    // (see ChatErrorCard). subscription_inactive is the remaining cross-route
    // CTA and exercises the identical Link path.
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="subscription_inactive" message="" />);
    const link = screen.getByRole("link", { name: "Reactivate" });
    expect(link).toHaveAttribute("href", "/settings?panel=billing");
    fireEvent.click(link, { button: 0 });
    expect(push).not.toHaveBeenCalled();
  });

  it("keeps pushState for in-shell CTAs", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="out_of_credits" message="" />);
    fireEvent.click(screen.getByRole("link", { name: "Top up now" }), { button: 0 });
    expect(push).toHaveBeenCalledWith(null, "", "/chat?panel=llm");
  });
});
