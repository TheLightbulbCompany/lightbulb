// @vitest-environment jsdom
import { afterEach, describe, it, expect, vi } from "vitest";
import * as React from "react";
import { render, screen } from "@testing-library/react";

import { ChatErrorCard } from "../ChatErrorCard";

const billing = vi.hoisted(() => ({ account: null as Record<string, unknown> | null }));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: billing.account }) }));

describe("ChatErrorCard", () => {
  it("links a trial cap to the org billing surface without a personal provider picker", () => {
    render(<ChatErrorCard code="trial_daily_cap" message="server copy" />);

    expect(screen.getByText("Daily free limit reached")).toBeInTheDocument();
    expect(screen.queryByText("server copy")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade to keep going" })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
    expect(screen.queryByText("Powered by Claude")).not.toBeInTheDocument();
  });

  it("uses an alert for trial errors now that no interactive form is embedded", () => {
    render(<ChatErrorCard code="trial_daily_cap" message="" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("links trial expiry to billing and omits a reset line", () => {
    render(<ChatErrorCard code="trial_expired" message="server copy: trial_expired" />);
    expect(screen.getByText("This workspace is reading-only")).toBeInTheDocument();
    expect(screen.queryByText("server copy: trial_expired")).not.toBeInTheDocument();
    expect(screen.queryByText(/resets at midnight/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /see the plan/i })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("renders out_of_credits with a top-up CTA instead of Failed to generate", () => {
    render(<ChatErrorCard code="out_of_credits" message="server copy: out_of_credits" />);
    expect(screen.getByText("Out of credits")).toBeInTheDocument();
    expect(
      screen.getByText("Your workspace credit balance is used up. Top up to keep your agents running."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Failed to generate")).not.toBeInTheDocument();
    expect(screen.queryByText("server copy: out_of_credits")).not.toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    // Routes to the managed-credit panel for either managed provider.
    expect(screen.getByRole("link", { name: "Top up now" })).toHaveAttribute(
      "href",
      "/chat?panel=llm",
    );
  });

  it("renders subscription_inactive with a reactivate CTA", () => {
    render(<ChatErrorCard code="subscription_inactive" message="server copy: subscription_inactive" />);
    expect(screen.getByText("Subscription inactive")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Your subscription isn't active, so agent runs are paused. Reactivate to continue.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Failed to generate")).not.toBeInTheDocument();
    expect(screen.queryByText("server copy: subscription_inactive")).not.toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reactivate" })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  /**
   * The card carried a Retry button behind `Boolean(onRetry)` and a
   * MONEY_CODES suppression list, and the only production caller
   * (MessageList) never passed a handler — so it had never rendered, not
   * once. It was deleted rather than wired: Retry is only truthful for
   * `timeout`, and reaching it needs a resend threaded four layers down
   * through MessageList, which has no send on its props at all.
   *
   * Cast past the prop type on purpose. A future caller can only resurrect
   * this button by passing a handler, so that is exactly what the guard has
   * to attempt.
   */
  it("has no Retry affordance, for any code, even when handed a handler", () => {
    const Card = ChatErrorCard as unknown as React.FC<Record<string, unknown>>;
    for (const code of [
      "run_error",
      "trial_daily_cap",
      "trial_expired",
      "out_of_credits",
      "subscription_inactive",
    ]) {
      const { unmount } = render(<Card code={code} message="" onRetry={vi.fn()} />);
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
      unmount();
    }
  });

  it("falls back to a generic title for unknown codes and shows the upstream message verbatim", () => {
    render(<ChatErrorCard code="weird_new_code" message="some upstream detail" />);
    expect(screen.getByText("Failed to generate")).toBeInTheDocument();
    expect(screen.getByText("some upstream detail")).toBeInTheDocument();
  });

  it("falls back to a generic message when code is omitted", () => {
    render(<ChatErrorCard message="" />);
    expect(screen.getByText("Failed to generate")).toBeInTheDocument();
    expect(
      screen.getByText("Your agent encountered an error. Please try again."),
    ).toBeInTheDocument();
  });

  it("attaches the code as a data attribute for analytics / e2e", () => {
    render(<ChatErrorCard code="run_error" message="m" />);
    expect(screen.getByTestId("chat-error-card")).toHaveAttribute(
      "data-error-code",
      "run_error",
    );
  });

  describe("included_daily_cap — a PAYING workspace's daily allowance", () => {
    const paid = (plan: string, provider_choice: string | null = null) => ({
      is_subscribed: true, subscription_status: "active", plan, provider_choice,
    });
    const TITLE = "Today's included usage is used up";
    const RESET = "You've used today's included usage. It resets at 00:00 UTC.";
    const expectNoTrialCopy = () => {
      expect(screen.queryByText(/free/i)).not.toBeInTheDocument();
      expect(screen.queryByText("Daily free limit reached")).not.toBeInTheDocument();
    };

    afterEach(() => { billing.account = null; });

    it("points Base at the Workspace upgrade", () => {
      billing.account = paid("base");
      render(<ChatErrorCard code="included_daily_cap" message="server copy" />);
      expect(screen.getByText(TITLE)).toBeInTheDocument();
      expect(screen.getByText(new RegExp(`${RESET.replace(/[.]/g, "\\.")}`))).toBeInTheDocument();
      expect(screen.getByText(/Upgrade to Workspace for pay-as-you-go credits/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Upgrade" })).toHaveAttribute("href", "/settings?panel=billing");
      expectNoTrialCopy();
    });

    it.each(["standard", "ultra_pro"])("points a %s workspace with no provider at the provider picker", (plan) => {
      billing.account = paid(plan);
      render(<ChatErrorCard code="included_daily_cap" message="server copy" />);
      expect(screen.getByText(TITLE)).toBeInTheDocument();
      expect(screen.getByText(/Sign in with ChatGPT or choose OpenRouter managed/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Pick provider" })).toHaveAttribute(
        "href",
        "/settings?panel=billing",
      );
      expectNoTrialCopy();
    });

    it("falls back to the server's neutral copy and Billing otherwise", () => {
      billing.account = paid("standard", "bedrock_claude");
      render(<ChatErrorCard code="included_daily_cap" message="Neutral server text." />);
      expect(screen.getByText(TITLE)).toBeInTheDocument();
      expect(screen.getByText("Neutral server text.")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Billing" })).toHaveAttribute("href", "/settings?panel=billing");
      expectNoTrialCopy();
    });

    it("never shows trial copy while billing is still unknown", () => {
      render(<ChatErrorCard code="included_daily_cap" message="" />);
      expect(screen.getByText(TITLE)).toBeInTheDocument();
      expect(screen.getByText(RESET)).toBeInTheDocument();
      expectNoTrialCopy();
    });

    it("renders a legacy trial_daily_cap for a PAYING workspace as included usage, not the free limit", () => {
      billing.account = paid("base");
      render(<ChatErrorCard code="trial_daily_cap" message="You've used today's free allowance" />);
      expect(screen.getByText(TITLE)).toBeInTheDocument();
      expectNoTrialCopy();
    });

    it("keeps the free-limit card for an actual trial", () => {
      billing.account = { is_subscribed: false, subscription_status: "trial_local", plan: "standard", provider_choice: null };
      render(<ChatErrorCard code="trial_daily_cap" message="" />);
      expect(screen.getByText("Daily free limit reached")).toBeInTheDocument();
    });
  });
});
