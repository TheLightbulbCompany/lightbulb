// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChatErrorCard } from "../ChatErrorCard";

describe("ChatErrorCard", () => {
  it("renders the rate_limit variant with the three-remedies copy and Switch provider action", () => {
    render(<ChatErrorCard code="rate_limit" message="429 from upstream" />);
    expect(screen.getByText("Provider rate limit reached")).toBeInTheDocument();
    // Description spells out all three remedies so the user can choose:
    // wait, upgrade their provider plan, or switch to Bedrock.
    expect(screen.getByText(/wait for your quota to reset/i)).toBeInTheDocument();
    expect(screen.getByText(/upgrade your plan with the provider/i)).toBeInTheDocument();
    expect(screen.getByText(/Bedrock-managed credits/i)).toBeInTheDocument();
    // Upstream "The usage limit has been reached." is intentionally NOT shown —
    // it's not actionable and would compete with our copy.
    expect(screen.queryByText("429 from upstream")).not.toBeInTheDocument();
    // ``action=change-provider`` is required so LLMPanel auto-opens the
    // change-provider modal on landing — without it the user lands on a
    // static panel and has to dig for the change button (the original
    // dead-end this card was created to fix).
    const link = screen.getByRole("link", { name: "Switch provider" });
    expect(link).toHaveAttribute("href", "/chat?panel=llm&action=change-provider");
  });

  it("renders the context_length variant with a change-provider action", () => {
    render(<ChatErrorCard code="context_length" message="" />);
    expect(screen.getByText("Conversation too long")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Change provider" });
    expect(link).toHaveAttribute("href", "/chat?panel=llm&action=change-provider");
  });

  it("renders the timeout variant with no action button", () => {
    render(<ChatErrorCard code="timeout" message="upstream timed out" />);
    expect(screen.getByText("Provider timeout")).toBeInTheDocument();
    expect(screen.getByText("upstream timed out")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders the refusal variant", () => {
    render(<ChatErrorCard code="refusal" message="" />);
    expect(screen.getByText("Request refused")).toBeInTheDocument();
  });

  it("renders the trial_daily_cap wall with an upgrade CTA and no dollar figure", () => {
    render(<ChatErrorCard code="trial_daily_cap" message="server copy" />);
    expect(screen.getByText("Daily free limit reached")).toBeInTheDocument();
    expect(screen.getByText(/resets at midnight UTC/i)).toBeInTheDocument();
    // No dollar figure at the wall: after the admission-threshold fix the wall
    // fires at $2.70 of usable spend while Settings advertises "$3/day", so any
    // number printed here would contradict it.
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    // The card renders from `code`, ignoring the server message, so the
    // pre-send and mid-run refusals are pixel-identical.
    expect(screen.queryByText("server copy")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade to keep going" })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("omits the reset line for trial_expired — expiry is permanent", () => {
    // Non-empty, distinctive message: proves the card ignores it (a
    // regressed `message ||` fallback would surface this string instead of
    // the hardcoded copy).
    render(<ChatErrorCard code="trial_expired" message="server copy: trial_expired" />);
    expect(screen.getByText("Your free trial has ended")).toBeInTheDocument();
    expect(
      screen.getByText("Your free trial has ended. Choose a plan to keep your agent running."),
    ).toBeInTheDocument();
    expect(screen.queryByText("server copy: trial_expired")).not.toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/resets at midnight/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /choose a plan/i })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("renders out_of_credits with a top-up CTA instead of Failed to generate", () => {
    render(<ChatErrorCard code="out_of_credits" message="server copy: out_of_credits" />);
    expect(screen.getByText("Out of credits")).toBeInTheDocument();
    expect(
      screen.getByText("Your Claude credit balance is used up. Top up to keep your agents running."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Failed to generate")).not.toBeInTheDocument();
    expect(screen.queryByText("server copy: out_of_credits")).not.toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    // Routes into CreditsStep, the same destination OutOfCreditsBanner uses.
    expect(screen.getByRole("link", { name: "Top up now" })).toHaveAttribute(
      "href",
      "/chat?provider=bedrock_claude",
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

  it("suppresses Retry on money codes but keeps it elsewhere", () => {
    const onRetry = vi.fn();
    for (const code of ["trial_daily_cap", "trial_expired", "out_of_credits", "subscription_inactive"]) {
      const { unmount } = render(<ChatErrorCard code={code} message="" onRetry={onRetry} />);
      // Retry against a budget wall is guaranteed to fail until reset or
      // top-up; offering it reads as product flakiness, not an exhausted
      // allowance.
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
      unmount();
    }
    render(<ChatErrorCard code="timeout" message="" onRetry={onRetry} />);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
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

  it("renders a retry button when onRetry is provided", () => {
    const onRetry = vi.fn();
    render(<ChatErrorCard code="rate_limit" message="rate limited" onRetry={onRetry} />);
    const retryBtn = screen.getByRole("button", { name: /retry/i });
    retryBtn.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("attaches the code as a data attribute for analytics / e2e", () => {
    render(<ChatErrorCard code="rate_limit" message="m" />);
    expect(screen.getByTestId("chat-error-card")).toHaveAttribute(
      "data-error-code",
      "rate_limit",
    );
  });

  it("never renders the scary 'Provider timeout' title for a stray lock-style error", () => {
    // The backend serialize gate makes a raw "session file locked" reaching the
    // web client a now-impossible/defensive path. Even on an untested edge, a
    // lock error carries no OpenClaw errorKind — so it would never reach this
    // card with the `timeout` code. Assert that a lock message under a
    // non-timeout code (or no code) is NOT presented as "Provider timeout".
    render(<ChatErrorCard message="session file locked" />);
    expect(screen.queryByText("Provider timeout")).not.toBeInTheDocument();
    expect(screen.getByText("Failed to generate")).toBeInTheDocument();
  });
});
