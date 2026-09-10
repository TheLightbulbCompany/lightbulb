// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import * as React from "react";
import { render, screen } from "@testing-library/react";

const billing = vi.hoisted(() => ({
  startTrialCheckout: vi.fn(),
  account: { workspace_price_usd: 50, ultra_price_usd: 150 },
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => billing,
}));

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

  it("converts trial_daily_cap in place instead of linking out to settings", () => {
    // The paywall renders HERE, in the transcript where the run stopped.
    //
    // It used to be a red error card whose CTA cross-routed to
    // /settings?panel=billing, and for a mid-session owner that was the ONLY
    // paywall reachable at all: useOnboardingStatus polls "0/off once ready",
    // so nothing re-asks the gate after the shell settles and
    // ProvisioningStepper's own picker branch never fires. Flipping the gate
    // instead is not an option either — the stepper wraps {dock}, so a blocked
    // gate unmounts the whole workspace mid-session.
    render(<ChatErrorCard code="trial_daily_cap" message="server copy" />);

    expect(screen.getByText("You've used today's free usage")).toBeInTheDocument();
    // All three paths on offer, with their real prices — an owner whose free
    // usage ran out is exactly who should get the full choice.
    expect(screen.getByRole("button", { name: /powered by claude/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with chatgpt/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /bring your own api key/i })).toBeInTheDocument();
    // $70 is the pre-payment disclosure for the Bedrock path: its Checkout
    // bundles $20 of starter credits beside the $50 subscription, because
    // provision_gate won't start that container on a zero balance.
    expect(screen.getByText(/\$70 at checkout/i)).toBeInTheDocument();

    // The card renders from `code`, ignoring the server message, so the
    // pre-send and mid-run refusals are identical.
    expect(screen.queryByText("server copy")).not.toBeInTheDocument();
    // No bounce to settings — the whole point of converting in place.
    expect(screen.queryByRole("link", { name: "Upgrade to keep going" })).not.toBeInTheDocument();
  });

  it("suppresses the paywall on non-newest bubbles so purchase forms cannot stack", () => {
    // MessageList renders one card per errored message, and each ProviderPicker
    // owns its own credit amount and loading state. Stacked copies let an owner
    // set $100 on the visible card, scroll up, and buy an older card's $20 —
    // charged an amount the screen never quoted — with CTAs that do not disable
    // one another.
    render(<ChatErrorCard code="trial_daily_cap" message="" interactive={false} />);
    expect(screen.queryByRole("button", { name: /powered by claude/i })).not.toBeInTheDocument();
    // It still says what happened, and still offers a way forward.
    expect(screen.getByText("Daily free limit reached")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade to keep going" })).toBeInTheDocument();
  });

  it("does not wrap the purchase form in role=alert", () => {
    // role="alert" carries implicit aria-live="assertive" + aria-atomic, so
    // every credit-amount change re-announced the whole pricing table and cut
    // the screen-reader user off mid-interaction. Right for a static error,
    // wrong for a live form.
    render(<ChatErrorCard code="trial_daily_cap" message="" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "You've used today's free usage" })).toBeInTheDocument();
    // A genuine error still announces assertively.
    render(<ChatErrorCard code="timeout" message="upstream timed out" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("ignores Object.prototype keys arriving as an error code", () => {
    // Indexing a plain object literal with a server-supplied key resolves
    // inherited members to truthy functions, which sailed through the gate and
    // rendered a purchase form for a code that is not a trial code.
    for (const code of ["constructor", "toString", "hasOwnProperty"]) {
      const { unmount } = render(<ChatErrorCard code={code} message="boom" />);
      expect(screen.queryByRole("button", { name: /powered by claude/i })).not.toBeInTheDocument();
      expect(screen.getByText("Failed to generate")).toBeInTheDocument();
      unmount();
    }
  });

  it("converts trial_expired in place and omits the reset line — expiry is permanent", () => {
    // Non-empty, distinctive message: proves the card ignores it (a
    // regressed `message ||` fallback would surface this string instead of
    // the hardcoded copy).
    render(<ChatErrorCard code="trial_expired" message="server copy: trial_expired" />);
    expect(screen.getByText("Your free trial has ended")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /powered by claude/i })).toBeInTheDocument();
    expect(screen.queryByText("server copy: trial_expired")).not.toBeInTheDocument();
    // trial_cap._is_expired never self-clears, so no "resets at midnight"
    // here — that line belongs to the daily-cap variant only.
    expect(screen.queryByText(/resets at midnight/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /choose a plan/i })).not.toBeInTheDocument();
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
      "timeout",
      "rate_limit",
      "refusal",
      "context_length",
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
